"""
Name → CUI Matcher
===================
Takes a list of unstandardized Romanian company names and matches them
against the indexed OD_FIRME dataset (firme_fts) using a two-stage pipeline:

  1. RETRIEVAL — FTS5 full-text search on normalized tokens (fast, broad recall)
  2. SCORING   — rapidfuzz WRatio fuzzy comparison against candidates (precise)

Thresholds (fixed per product decision):
  score >= 97          -> auto-matched
  70 <= score < 97      -> pending manual review
  score < 70            -> no match found

Generic/short names (after stripping legal-form tokens) are always forced
into manual review regardless of score, since the false-positive risk is high
(e.g. "Total SRL" matching hundreds of companies).
"""
from __future__ import annotations
import asyncio, re, sqlite3, time, unicodedata, uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from rapidfuzz import fuzz, process

from config import Config
from backend.utils.logger import get_logger

log = get_logger(__name__)

AUTO_THRESHOLD   = 97.0
REVIEW_THRESHOLD = 70.0
MAX_CANDIDATES   = 20     # FTS candidates retrieved per name
MAX_ALTERNATIVES = 5      # kept for manual-review dropdown
GENERIC_MIN_LEN  = 4      # normalized name shorter than this -> forced review (e.g. "abc")
AMBIGUITY_GAP    = 3.0    # if the #2 candidate scores within this many points of #1,
                          # treat the match as ambiguous and force review regardless of score

# ── Legal-form tokens stripped during normalization ────────────────────
# Confirmed ONRC abbreviations + common written-out / punctuated variants.
_LEGAL_FORM_TOKENS = {
    "sc", "srl", "srld", "sa", "sca", "scs", "snc", "pfa", "pf", "ii", "if",
    "af", "ca", "scr", "ra", "oc", "ong",
}

_DIACRITIC_MAP = str.maketrans({
    "ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t",
    "Ă": "a", "Â": "a", "Î": "i", "Ș": "s", "Ş": "s", "Ț": "t", "Ţ": "t",
})

_DOT_RE   = re.compile(r"\.")                     # dots collapse (S.R.L. -> SRL, not "s r l")
_PUNCT_RE = re.compile(r"[\-,&/\\()\"'`_]+")       # other punctuation -> space
_WS_RE    = re.compile(r"\s+")


def normalize_name(raw: str) -> str:
    """Lowercase, strip diacritics/punctuation, remove legal-form tokens,
    collapse whitespace. Same function applied to both input names and
    ONRC denumire so comparisons are apples-to-apples.

    Dots are removed WITHOUT inserting a space so abbreviations like
    "S.R.L." or "S.C." collapse to "srl" / "sc" (single tokens) rather
    than splitting into individual letters "s r l"."""
    if not raw:
        return ""
    s = raw.strip().translate(_DIACRITIC_MAP).lower()
    s = _DOT_RE.sub("", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    tokens = [t for t in s.split(" ") if t and t not in _LEGAL_FORM_TOKENS]
    return " ".join(tokens)


def is_generic(normalized: str) -> bool:
    """True if the normalized name is too short to trust confidently
    (e.g. 'abc', 'srl' stripped down to nothing meaningful)."""
    return len(normalized) < GENERIC_MIN_LEN


def is_ambiguous(alternatives: list) -> bool:
    """True if the top two candidates are close enough in score that we
    can't confidently pick one automatically — e.g. 'Trans SRL' matching
    both 'Trans Expres SRL' and 'Trans Logistic SRL' at the same score.
    This is the real signal for forcing manual review, not name length —
    plenty of legitimate one-word brand names (Dedeman, Kaufland, Altex)
    have a single, unambiguous top candidate and shouldn't be flagged."""
    if len(alternatives) < 2:
        return False
    return (alternatives[0].score - alternatives[1].score) < AMBIGUITY_GAP


# ── SQLite candidate retrieval ──────────────────────────────────────────

def _fts_candidates(conn: sqlite3.Connection, normalized_query: str, limit: int = MAX_CANDIDATES) -> list[dict]:
    """OR-semantics prefix search — forgiving of typos/missing tokens.
    Retrieval only; scoring happens separately with rapidfuzz."""
    tokens = [t for t in normalized_query.split(" ") if len(t) >= 2]
    if not tokens:
        return []
    fts_q = " OR ".join(f'"{t}"*' for t in tokens[:8])  # cap tokens to keep query fast
    try:
        rows = conn.execute("""
            SELECT f.cui, f.denumire, f.cod_inmatriculare, f.forma_juridica,
                   f.adr_judet, f.adr_localitate, f.data_inmatriculare
            FROM firme f
            JOIN firme_fts ON f.id = firme_fts.rowid
            WHERE firme_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (fts_q, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception as ex:
        log.debug("FTS candidates error for %r: %s", normalized_query, ex)
        return []


# ── Result / Job dataclasses ────────────────────────────────────────────

MatchStatus = Literal["auto", "pending", "no_match", "approved", "rejected", "manual"]


@dataclass
class MatchAlternative:
    cui: int
    denumire: str
    score: float
    judet: str = ""
    forma_juridica: str = ""

    def to_dict(self) -> dict:
        return {"cui": self.cui, "denumire": self.denumire, "score": round(self.score, 1),
                "judet": self.judet, "forma_juridica": self.forma_juridica}


@dataclass
class MatchResult:
    row_index: int
    input_name: str
    normalized_input: str
    cui: int | None = None
    matched_name: str = ""
    score: float = 0.0
    status: MatchStatus = "no_match"
    forced_review: bool = False
    judet: str = ""
    forma_juridica: str = ""
    alternatives: list[MatchAlternative] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "row_index":       self.row_index,
            "input_name":      self.input_name,
            "cui":             self.cui,
            "matched_name":    self.matched_name,
            "score":           round(self.score, 1),
            "status":          self.status,
            "forced_review":   self.forced_review,
            "judet":           self.judet,
            "forma_juridica":  self.forma_juridica,
            "alternatives":    [a.to_dict() for a in self.alternatives],
        }


@dataclass
class MatchJob:
    job_id: str
    input_names: list[str]
    total: int = 0
    processed: int = 0
    status: str = "queued"     # queued | running | done | cancelled | error
    results: list[MatchResult] = field(default_factory=list)
    message: str = ""
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    _cancel_flag: bool = False
    _task: Any = None

    def __post_init__(self):
        self.total = len(self.input_names)

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    @property
    def summary(self) -> dict:
        counts = {"auto": 0, "pending": 0, "no_match": 0, "approved": 0, "rejected": 0, "manual": 0}
        for r in self.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        return counts

    def to_status_dict(self) -> dict:
        return {
            "job_id": self.job_id, "status": self.status, "total": self.total,
            "processed": self.processed, "message": self.message, "error": self.error,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "summary": self.summary if self.status == "done" else None,
        }


class MatchJobManager:
    def __init__(self):
        self._jobs: dict[str, MatchJob] = {}

    def create(self, names: list[str]) -> MatchJob:
        jid = str(uuid.uuid4())[:8]
        job = MatchJob(job_id=jid, input_names=names)
        self._jobs[jid] = job
        return job

    def get(self, jid: str) -> MatchJob | None:
        return self._jobs.get(jid)


_mgr = MatchJobManager()

def get_match_manager() -> MatchJobManager:
    return _mgr


# ── Matching engine ──────────────────────────────────────────────────────

def _match_one(conn: sqlite3.Connection, row_index: int, raw_name: str) -> MatchResult:
    norm = normalize_name(raw_name)
    if not norm:
        return MatchResult(row_index=row_index, input_name=raw_name, normalized_input=norm,
                           status="no_match")

    candidates = _fts_candidates(conn, norm)
    if not candidates:
        return MatchResult(row_index=row_index, input_name=raw_name, normalized_input=norm,
                           status="no_match")

    # Build normalized candidate texts once, score with rapidfuzz WRatio
    cand_norms = [normalize_name(c["denumire"]) for c in candidates]
    scored = process.extract(norm, cand_norms, scorer=fuzz.WRatio, limit=MAX_ALTERNATIVES)
    # scored: list of (matched_text, score, index_in_cand_norms)

    if not scored:
        return MatchResult(row_index=row_index, input_name=raw_name, normalized_input=norm,
                           status="no_match")

    alternatives = []
    seen_cuis = set()
    for _, score, idx in scored:
        c = candidates[idx]
        if c["cui"] in seen_cuis:
            continue
        seen_cuis.add(c["cui"])
        alternatives.append(MatchAlternative(
            cui=c["cui"], denumire=c["denumire"], score=score,
            judet=c.get("adr_judet") or "", forma_juridica=c.get("forma_juridica") or "",
        ))

    if not alternatives:
        return MatchResult(row_index=row_index, input_name=raw_name, normalized_input=norm,
                           status="no_match")

    best = alternatives[0]
    forced = is_generic(norm) or is_ambiguous(alternatives)

    if best.score >= AUTO_THRESHOLD and not forced:
        status: MatchStatus = "auto"
    elif best.score >= REVIEW_THRESHOLD or forced:
        status = "pending"
    else:
        status = "no_match"

    return MatchResult(
        row_index=row_index, input_name=raw_name, normalized_input=norm,
        cui=best.cui if status != "no_match" else None,
        matched_name=best.denumire if status != "no_match" else "",
        score=best.score, status=status, forced_review=forced,
        judet=best.judet, forma_juridica=best.forma_juridica,
        alternatives=alternatives,
    )


def _match_chunk(names_chunk: list[tuple[int, str]]) -> list[MatchResult]:
    """Runs in a thread executor — opens its own SQLite connection."""
    if not Config.ONRC_DB_PATH.exists():
        return [MatchResult(row_index=i, input_name=n, normalized_input=normalize_name(n),
                            status="no_match") for i, n in names_chunk]
    conn = sqlite3.connect(str(Config.ONRC_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return [_match_one(conn, i, n) for i, n in names_chunk]
    finally:
        conn.close()


async def run_match_job(job: MatchJob):
    job.status = "running"
    job.started_at = time.time()
    job.message = f"Matching {job.total:,} names against ONRC…"

    from backend.services.onrc_service import get_onrc
    onrc = get_onrc()
    if not onrc.is_indexed():
        job.status = "error"
        job.error = "ONRC not indexed — index ONRC data first"
        job.finished_at = time.time()
        return

    CHUNK = 200
    loop = asyncio.get_event_loop()
    pairs = list(enumerate(job.input_names))

    for start in range(0, len(pairs), CHUNK):
        if job._cancel_flag:
            job.status = "cancelled"
            job.finished_at = time.time()
            return
        chunk = pairs[start:start + CHUNK]
        chunk_results = await loop.run_in_executor(None, _match_chunk, chunk)
        job.results.extend(chunk_results)
        job.processed = len(job.results)
        job.message = f"Matched {job.processed:,}/{job.total:,}…"

    job.finished_at = time.time()
    job.status = "done"
    s = job.summary
    job.message = (
        f"✅ Done — {s['auto']:,} auto-matched · {s['pending']:,} need review · "
        f"{s['no_match']:,} no match"
    )
    log.info("Name match job %s: %s", job.job_id, s)
