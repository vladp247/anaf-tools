"""
ONRC Open Data Service
======================
Indexes OD_FIRME.csv, OD_STARE_FIRMA.csv, OD_REPREZENTANTI_LEGALI.csv,
OD_REPREZENTANTI_IF.csv (representatives/members of întreprinderi
individuale/familiale — II/IF entities, a separate ONRC export from the
legal-reps file used for SRL/SA/etc.) and nomenclator.csv into a local
SQLite database. Both representative files load into the same
`reprezentanti` table (keyed by cod_inmatriculare) since callers only
ever query representatives generically by that key.

All files use '^' as delimiter except nomenclator.csv which uses '|'.
Files go in data/ folder (project root). Nomenclator goes in project root.
"""
from __future__ import annotations
import csv
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from config import Config
from backend.utils.logger import get_logger

log = get_logger(__name__)


def _resolve_path(expected: Path) -> Path:
    """
    Return the actual path on disk, resolving case-insensitively.
    Handles: uppercase .CSV, .csv.csv double-extension, browser " (1).csv" duplicates.
    """
    if expected.exists():
        return expected
    if not expected.parent.exists():
        return expected

    target = expected.name.lower()   # e.g. "od_reprezentanti_legali.csv"
    stem   = expected.stem.lower()   # e.g. "od_reprezentanti_legali"

    for f in expected.parent.iterdir():
        fl = f.name.lower()

        # Pass 1: exact case-insensitive name match
        if fl == target:
            log.info("Case match: %s → %s", expected.name, f.name)
            return f

        # Pass 2: double-extension (.csv.csv) — stem of file = expected full name
        # e.g. "OD_REPREZENTANTI_LEGALI.csv.csv" has stem "OD_REPREZENTANTI_LEGALI.csv"
        if fl == target + '.csv' or f.stem.lower() == target:
            log.info("Double-ext match: %s → %s", expected.name, f.name)
            return f

        # Pass 3: correct stem, correct extension (handles uppercase .CSV)
        if f.stem.lower() == stem and f.suffix.lower() == '.csv':
            log.info("Stem match: %s → %s", expected.name, f.name)
            return f

    # Pass 4: browser duplicate " (1).csv"
    import re as _re
    suffix_re = _re.compile(r'^' + _re.escape(stem) + r'[\s()\d]*$')
    for f in expected.parent.iterdir():
        if suffix_re.match(f.stem.lower()) and f.suffix.lower() == '.csv':
            log.info("Partial match: %s → %s", expected.name, f.name)
            return f

    return expected

# ── Global indexing progress ──────────────────────────────────────────────────
_state: dict = {
    "status": "idle",   # idle | indexing | done | error
    "phase": "",
    "current": 0,
    "total": 0,
    "message": "",
    "error": None,
}
_state_lock = threading.Lock()


def _upd(**kw):
    with _state_lock:
        _state.update(kw)


def get_index_state() -> dict:
    with _state_lock:
        return dict(_state)


# ── Service ───────────────────────────────────────────────────────────────────

class ONRCService:
    def __init__(self):
        Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._nom: dict[int, str] | None = None
        self._nom_lock = threading.Lock()

    # ── File status ──────────────────────────────────────────────────────────

    def files_status(self) -> dict:
        firme  = _resolve_path(Config.ONRC_FIRME_PATH).exists()
        stare  = _resolve_path(Config.ONRC_STARE_PATH).exists()
        reps   = _resolve_path(Config.ONRC_REPS_PATH).exists()
        reps_if = _resolve_path(Config.ONRC_REPS_IF_PATH).exists()
        nom    = _resolve_path(Config.NOMENCLATOR_PATH).exists()
        return {
            "firme": firme,
            "stare": stare,
            "reps": reps,
            "reps_if": reps_if,
            "nomenclator": nom,
            "all_csv": firme and stare and reps,
        }

    def is_indexed(self) -> bool:
        if not Config.ONRC_DB_PATH.exists():
            return False
        try:
            with self._conn() as c:
                n = c.execute("SELECT COUNT(*) FROM firme").fetchone()[0]
            return n > 0
        except Exception:
            return False

    def db_stats(self) -> dict:
        if not self.is_indexed():
            return {}
        try:
            with self._conn() as c:
                return {
                    "companies": c.execute("SELECT COUNT(*) FROM firme").fetchone()[0],
                    "statuses":  c.execute("SELECT COUNT(*) FROM stare_firma").fetchone()[0],
                    "reps":      c.execute("SELECT COUNT(*) FROM reprezentanti").fetchone()[0],
                }
        except Exception:
            return {}

    # ── Nomenclator ──────────────────────────────────────────────────────────

    def load_nomenclator(self) -> dict[int, str]:
        with self._nom_lock:
            if self._nom is not None:
                return self._nom
            path = _resolve_path(Config.NOMENCLATOR_PATH)
            if not path.exists():
                self._nom = {}
                return {}
            result: dict[int, str] = {}
            # ONRC nomenclator files are Windows Romanian (CP1250) — try that first
            # to avoid UTF-8 mis-decoding Romanian diacritics (ș ț ă â î) as garbled chars
            for enc in ("cp1250", "utf-8-sig", "utf-8", "latin-1"):
                try:
                    with open(path, encoding=enc, newline="", errors="replace") as f:
                        reader = csv.DictReader(f, delimiter="|")
                        for row in reader:
                            try:
                                cod = int((row.get("COD") or "").strip())
                                den = (row.get("DENUMIRE") or "").strip()
                                result[cod] = den
                            except (ValueError, AttributeError):
                                continue
                    break
                except Exception:
                    continue
            self._nom = result
            log.info("Nomenclator loaded: %d entries", len(result))
            return result

    def decode_status(self, cod: int) -> str:
        return self.load_nomenclator().get(cod, f"Cod {cod}")

    # ── DB connection ────────────────────────────────────────────────────────

    def _conn(self):
        """Return a context manager that opens and ALWAYS closes the connection."""
        path = str(Config.ONRC_DB_PATH)
        class _Ctx:
            def __enter__(ctx):
                ctx._c = sqlite3.connect(path, timeout=30, check_same_thread=False)
                ctx._c.row_factory = sqlite3.Row
                return ctx._c
            def __exit__(ctx, *_):
                try: ctx._c.close()
                except Exception: pass
        return _Ctx()

    # ── Indexing ─────────────────────────────────────────────────────────────

    def start_indexing(self) -> bool:
        """Launch background indexing thread. Returns False if already running."""
        if get_index_state()["status"] == "indexing":
            return False
        t = threading.Thread(target=self._worker, daemon=True, name="onrc-indexer")
        t.start()
        return True

    def _worker(self):
        _upd(status="indexing", phase="init", current=0, total=0, message="Starting…", error=None)
        try:
            self._do_index()
            _upd(status="done")
        except Exception as ex:
            log.error("ONRC indexing error: %s", ex, exc_info=True)
            _upd(status="error", error=str(ex), message=str(ex))

    @staticmethod
    def _try_open(path: Path, delimiter: str):
        """Open CSV with multiple encoding attempts, return (file_handle, reader) or (None, None)."""
        for enc in ("utf-8-sig", "utf-8", "cp1250", "latin-1"):
            try:
                f = open(path, encoding=enc, newline="", errors="replace")
                reader = csv.DictReader(f, delimiter=delimiter)
                peek = next(iter(reader), None)
                if peek is None:
                    f.close()
                    continue
                f.seek(0)
                return f, csv.DictReader(f, delimiter=delimiter)
            except Exception:
                continue
        return None, None

    @staticmethod
    def _line_count(path: Path) -> int:
        for enc in ("utf-8-sig", "utf-8", "cp1250", "latin-1"):
            try:
                with open(path, encoding=enc, errors="replace") as f:
                    return max(sum(1 for _ in f) - 1, 0)
            except Exception:
                continue
        return 0

    def _do_index(self):
        db = str(Config.ONRC_DB_PATH)
        # Do NOT unlink — Windows holds a file lock on open SQLite DBs.
        # Drop and recreate tables instead (equivalent to a fresh DB).
        conn = sqlite3.connect(db, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=100000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.executescript("""
            DROP TABLE IF EXISTS firme_fts;
            DROP TABLE IF EXISTS firme;
            DROP TABLE IF EXISTS stare_firma;
            DROP TABLE IF EXISTS reprezentanti;
        """)
        conn.commit()

        # ── Create tables ────────────────────────────────────────────
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS firme (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                denumire TEXT NOT NULL DEFAULT '',
                cui INTEGER NOT NULL DEFAULT 0,
                cod_inmatriculare TEXT DEFAULT '',
                data_inmatriculare TEXT DEFAULT '',
                euid TEXT DEFAULT '',
                forma_juridica TEXT DEFAULT '',
                adr_tara TEXT DEFAULT '',
                adr_judet TEXT DEFAULT '',
                adr_localitate TEXT DEFAULT '',
                adr_den_strada TEXT DEFAULT '',
                adr_nr_strada TEXT DEFAULT '',
                adr_bloc TEXT DEFAULT '',
                adr_scara TEXT DEFAULT '',
                adr_etaj TEXT DEFAULT '',
                adr_apartament TEXT DEFAULT '',
                adr_cod_postal TEXT DEFAULT '',
                adr_sector TEXT DEFAULT '',
                adr_completare TEXT DEFAULT '',
                web TEXT DEFAULT '',
                tara_firma_mama TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_f_cui ON firme(cui);
            CREATE INDEX IF NOT EXISTS idx_f_cod ON firme(cod_inmatriculare);

            CREATE TABLE IF NOT EXISTS stare_firma (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cod_inmatriculare TEXT NOT NULL,
                cod INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_s_cod ON stare_firma(cod_inmatriculare);

            CREATE TABLE IF NOT EXISTS reprezentanti (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cod_inmatriculare TEXT NOT NULL,
                persoana TEXT DEFAULT '',
                calitate TEXT DEFAULT '',
                data_nastere TEXT DEFAULT '',
                loc_nastere TEXT DEFAULT '',
                jud_nastere TEXT DEFAULT '',
                tara_nastere TEXT DEFAULT '',
                localitate TEXT DEFAULT '',
                judet TEXT DEFAULT '',
                tara TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_r_cod ON reprezentanti(cod_inmatriculare);
        """)
        conn.commit()

        # ── Phase 1: OD_FIRME ────────────────────────────────────────
        firme_path = _resolve_path(Config.ONRC_FIRME_PATH)
        _upd(phase="firme", message="Counting companies…")
        total = self._line_count(firme_path)
        _upd(total=total, message=f"Loading {total:,} companies…")

        fh, reader = self._try_open(firme_path, "^")
        n = 0
        batch = []
        INSERT_FIRME = (
            "INSERT INTO firme VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        if reader:
            for row in reader:
                try:
                    raw_cui = (row.get("CUI") or "").strip()
                    cui = int(raw_cui) if raw_cui and raw_cui.lstrip("-").isdigit() else 0
                    batch.append((
                        (row.get("DENUMIRE") or "").strip(),
                        cui,
                        (row.get("COD_INMATRICULARE") or "").strip(),
                        (row.get("DATA_INMATRICULARE") or "").strip(),
                        (row.get("EUID") or "").strip(),
                        (row.get("FORMA_JURIDICA") or "").strip(),
                        (row.get("ADR_TARA") or "").strip(),
                        (row.get("ADR_JUDET") or "").strip(),
                        (row.get("ADR_LOCALITATE") or "").strip(),
                        (row.get("ADR_DEN_STRADA") or "").strip(),
                        (row.get("ADR_NR_STRADA") or "").strip(),
                        (row.get("ADR_BLOC") or "").strip(),
                        (row.get("ADR_SCARA") or "").strip(),
                        (row.get("ADR_ETAJ") or "").strip(),
                        (row.get("ADR_APARTAMENT") or "").strip(),
                        (row.get("ADR_COD_POSTAL") or "").strip(),
                        (row.get("ADR_SECTOR") or "").strip(),
                        (row.get("ADR_COMPLETARE") or "").strip(),
                        (row.get("WEB") or "").strip(),
                        (row.get("TARA_FIRMA_MAMA") or "").strip(),
                    ))
                    n += 1
                    if len(batch) >= 5000:
                        conn.executemany(INSERT_FIRME, batch)
                        conn.commit()
                        batch.clear()
                        _upd(current=n, message=f"Loaded {n:,} companies…")
                except Exception:
                    continue
            if batch:
                conn.executemany(INSERT_FIRME, batch)
                conn.commit()
            fh.close()
        log.info("OD_FIRME: %d rows", n)

        # ── Build FTS5 ───────────────────────────────────────────────
        _upd(phase="fts", current=n, message=f"Building search index for {n:,} companies…")
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS firme_fts
            USING fts5(denumire, content='firme', content_rowid='id')
        """)
        conn.execute("INSERT INTO firme_fts(firme_fts) VALUES('rebuild')")
        conn.commit()
        log.info("FTS5 index built")

        # ── Phase 2: OD_STARE_FIRMA ──────────────────────────────────
        stare_path = _resolve_path(Config.ONRC_STARE_PATH)
        _upd(phase="stare", current=0, message="Loading company status records…")
        total_s = self._line_count(stare_path)
        _upd(total=total_s)
        fh, reader = self._try_open(stare_path, "^")
        ns = 0
        batch = []
        if reader:
            for row in reader:
                try:
                    cod_inm = (row.get("COD_INMATRICULARE") or "").strip()
                    cod_val = int((row.get("COD") or "").strip())
                    if cod_inm and cod_val:
                        batch.append((cod_inm, cod_val))
                        ns += 1
                        if len(batch) >= 5000:
                            conn.executemany(
                                "INSERT INTO stare_firma VALUES(NULL,?,?)", batch
                            )
                            conn.commit()
                            batch.clear()
                            _upd(current=ns, message=f"Status records: {ns:,}")
                except (ValueError, Exception):
                    continue
            if batch:
                conn.executemany("INSERT INTO stare_firma VALUES(NULL,?,?)", batch)
                conn.commit()
            fh.close()
        log.info("OD_STARE_FIRMA: %d rows", ns)

        # ── Phase 3: OD_REPREZENTANTI ────────────────────────────────
        reps_path = _resolve_path(Config.ONRC_REPS_PATH)
        _upd(phase="reps", current=0, message="Loading legal representatives…")
        total_r = self._line_count(reps_path)
        _upd(total=total_r)
        fh, reader = self._try_open(reps_path, "^")
        nr = 0
        batch = []
        if reader:
            for row in reader:
                try:
                    cod_inm = (row.get("COD_INMATRICULARE") or "").strip()
                    if not cod_inm:
                        continue
                    batch.append((
                        cod_inm,
                        (row.get("PERSOANA_IMPUTERNICITA") or "").strip(),
                        (row.get("CALITATE") or "").strip(),
                        (row.get("DATA_NASTERE") or "").strip(),
                        (row.get("LOCALITATE_NASTERE") or "").strip(),
                        (row.get("JUDET_NASTERE") or "").strip(),
                        (row.get("TARA_NASTERE") or "").strip(),
                        (row.get("LOCALITATE") or "").strip(),
                        (row.get("JUDET") or "").strip(),
                        (row.get("TARA") or "").strip(),
                    ))
                    nr += 1
                    if len(batch) >= 5000:
                        conn.executemany(
                            "INSERT INTO reprezentanti VALUES(NULL,?,?,?,?,?,?,?,?,?,?)",
                            batch,
                        )
                        conn.commit()
                        batch.clear()
                        _upd(current=nr, message=f"Representatives: {nr:,}")
                except Exception:
                    continue
            if batch:
                conn.executemany(
                    "INSERT INTO reprezentanti VALUES(NULL,?,?,?,?,?,?,?,?,?,?)", batch
                )
                conn.commit()
            fh.close()
        log.info("OD_REPREZENTANTI: %d rows", nr)

        # ── Phase 4: OD_REPREZENTANTI_IF (întreprinderi individuale/familiale) ──
        # Different column layout: NUME instead of PERSOANA_IMPUTERNICITA, no
        # current-residence localitate/judet/tara. Loads into the same table.
        reps_if_path = _resolve_path(Config.ONRC_REPS_IF_PATH)
        _upd(phase="reps_if", current=0, message="Loading II/IF representatives…")
        total_rif = self._line_count(reps_if_path)
        _upd(total=total_rif)
        fh, reader = self._try_open(reps_if_path, "^")
        nrif = 0
        batch = []
        if reader:
            for row in reader:
                try:
                    cod_inm = (row.get("COD_INMATRICULARE") or "").strip()
                    if not cod_inm:
                        continue
                    batch.append((
                        cod_inm,
                        (row.get("NUME") or "").strip(),
                        (row.get("CALITATE") or "").strip(),
                        (row.get("DATA_NASTERE") or "").strip(),
                        (row.get("LOCALITATE_NASTERE") or "").strip(),
                        (row.get("JUDET_NASTERE") or "").strip(),
                        (row.get("TARA_NASTERE") or "").strip(),
                        "", "", "",
                    ))
                    nrif += 1
                    if len(batch) >= 5000:
                        conn.executemany(
                            "INSERT INTO reprezentanti VALUES(NULL,?,?,?,?,?,?,?,?,?,?)",
                            batch,
                        )
                        conn.commit()
                        batch.clear()
                        _upd(current=nrif, message=f"II/IF representatives: {nrif:,}")
                except Exception:
                    continue
            if batch:
                conn.executemany(
                    "INSERT INTO reprezentanti VALUES(NULL,?,?,?,?,?,?,?,?,?,?)", batch
                )
                conn.commit()
            fh.close()
        log.info("OD_REPREZENTANTI_IF: %d rows", nrif)

        conn.execute("PRAGMA synchronous=NORMAL")
        conn.close()
        _upd(
            status="done",
            message=(
                f"Ready — {n:,} companies · {ns:,} status records · "
                f"{nr + nrif:,} representatives ({nr:,} legal + {nrif:,} II/IF)"
            ),
            current=n, total=n,
        )
        log.info("ONRC indexing complete")

    # ── Query ─────────────────────────────────────────────────────────────────

    def search_by_name(self, query: str, limit: int = 20) -> list[dict]:
        if not query or len(query) < 2 or not self.is_indexed():
            return []
        terms = query.strip().split()
        fts_q = " ".join(f'"{t}"*' for t in terms if t)
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT f.id, f.cui, f.denumire, f.cod_inmatriculare,
                           f.forma_juridica, f.adr_judet, f.adr_localitate,
                           f.data_inmatriculare
                    FROM firme f
                    JOIN firme_fts ON f.id = firme_fts.rowid
                    WHERE firme_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (fts_q, limit)).fetchall()
            return [dict(r) for r in rows]
        except Exception as ex:
            log.warning("FTS search error (%s), falling back to LIKE", ex)
        # LIKE fallback
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT id, cui, denumire, cod_inmatriculare,
                           forma_juridica, adr_judet, adr_localitate, data_inmatriculare
                    FROM firme WHERE denumire LIKE ? LIMIT ?
                """, (f"%{query}%", limit)).fetchall()
            return [dict(r) for r in rows]
        except Exception as ex2:
            log.error("LIKE fallback failed: %s", ex2)
            return []

    def get_by_cui(self, cui: int) -> dict | None:
        if not cui or not self.is_indexed():
            return None
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM firme WHERE cui=? LIMIT 1", (cui,)
                ).fetchone()
            return dict(row) if row else None
        except Exception as ex:
            log.warning("get_by_cui(%d): %s", cui, ex)
            return None

    def get_by_cod(self, cod: str) -> dict | None:
        if not cod or not self.is_indexed():
            return None
        cod = cod.strip()
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM firme WHERE cod_inmatriculare=? LIMIT 1", (cod,)
                ).fetchone()
            return dict(row) if row else None
        except Exception as ex:
            log.warning("get_by_cod(%s): %s", cod, ex)
            return None

    def get_stare(self, cod_inmatriculare: str) -> list[dict]:
        if not cod_inmatriculare or not self.is_indexed():
            return []
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT cod FROM stare_firma WHERE cod_inmatriculare=?",
                    (cod_inmatriculare.strip(),),
                ).fetchall()
            nom = self.load_nomenclator()
            return [{"cod": r["cod"], "denumire": nom.get(r["cod"], f"Cod {r['cod']}")} for r in rows]
        except Exception as ex:
            log.warning("get_stare(%s): %s", cod_inmatriculare, ex)
            return []

    def get_reprezentanti(self, cod_inmatriculare: str) -> list[dict]:
        if not cod_inmatriculare or not self.is_indexed():
            return []
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT persoana, calitate, data_nastere,
                           loc_nastere, jud_nastere, tara_nastere,
                           localitate, judet, tara
                    FROM reprezentanti
                    WHERE cod_inmatriculare=?
                    ORDER BY calitate
                """, (cod_inmatriculare.strip(),)).fetchall()
            return [dict(r) for r in rows]
        except Exception as ex:
            log.warning("get_reprezentanti(%s): %s", cod_inmatriculare, ex)
            return []

    def enrich(self, result: dict) -> dict:
        """Add ONRC data to a normalized result dict. Non-blocking."""
        if not self.is_indexed():
            result["onrc"] = None
            return result
        cui = result.get("cui", 0)
        co  = result.get("company") or {}
        nr  = co.get("registration_number", "")

        firma = None
        if nr:
            firma = self.get_by_cod(nr)
        if not firma and cui:
            firma = self.get_by_cui(cui)

        if firma:
            cod_inm = firma.get("cod_inmatriculare", "")
            result["onrc"] = {
                "firma": {k: v for k, v in firma.items() if v and k != "id"},
                "stare": self.get_stare(cod_inm),
                "reprezentanti": self.get_reprezentanti(cod_inm),
            }
        else:
            result["onrc"] = None

        return result

    def enrich_stare_only(self, result: dict) -> dict:
        """Lightweight ONRC enrichment for bulk: only stare_firma."""
        if not self.is_indexed():
            return result
        co = result.get("company") or {}
        nr = co.get("registration_number", "")
        if not nr:
            return result
        stare = self.get_stare(nr)
        if stare:
            result["onrc_stare"] = stare
        return result

    def enrich_bulk_batch(self, results: list[dict]) -> list[dict]:
        """Enrich a batch of results with ONRC firma data.
        Chunked to stay under SQLite's 999 bind-variable limit."""
        if not self.is_indexed() or not results:
            return results

        cuis = [r.get("cui", 0) for r in results if r.get("cui")]
        if not cuis:
            return results

        firma_by_cui: dict[int, dict] = {}
        stare_by_cod: dict[str, list] = {}
        CHUNK = 500

        try:
            with self._conn() as c:
                # Firma lookup — chunked
                for i in range(0, len(cuis), CHUNK):
                    chunk = cuis[i:i + CHUNK]
                    ph = ",".join("?" * len(chunk))
                    rows = c.execute(
                        f"""SELECT cui, cod_inmatriculare, denumire, forma_juridica,
                                   data_inmatriculare, adr_judet, adr_localitate,
                                   adr_den_strada, adr_nr_strada, adr_cod_postal, web,
                                   tara_firma_mama
                            FROM firme WHERE cui IN ({ph})""",
                        chunk
                    ).fetchall()
                    for row in rows:
                        firma_by_cui[row[0]] = dict(row)

                # Stare lookup — chunked by cod_inmatriculare
                cod_list = [firma_by_cui[c]["cod_inmatriculare"]
                            for c in cuis if c in firma_by_cui
                            and firma_by_cui[c].get("cod_inmatriculare")]
                for i in range(0, len(cod_list), CHUNK):
                    chunk_cod = cod_list[i:i + CHUNK]
                    ph2 = ",".join("?" * len(chunk_cod))
                    stare_rows = c.execute(
                        f"""SELECT cod_inmatriculare, cod
                            FROM stare_firma WHERE cod_inmatriculare IN ({ph2})""",
                        chunk_cod
                    ).fetchall()
                    for row in stare_rows:
                        cod_inm = row[0]
                        cod_val = row[1]
                        if cod_inm not in stare_by_cod:
                            stare_by_cod[cod_inm] = []
                        stare_by_cod[cod_inm].append({
                            "cod_inmatriculare": cod_inm,
                            "denumire": self._nom.get(cod_val, f"Cod {cod_val}"),
                            "cod": cod_val,
                        })
        except Exception as ex:
            log.warning("enrich_bulk_batch: %s", ex)
            return results

        for result in results:
            cui = result.get("cui", 0)
            if cui in firma_by_cui:
                firma = firma_by_cui[cui]
                cod_inm = firma.get("cod_inmatriculare", "")
                result["onrc_data"] = {
                    "cod_inmatriculare": cod_inm,
                    "forma_juridica":    firma.get("forma_juridica", ""),
                    "data_inmatriculare": firma.get("data_inmatriculare", ""),
                    "adr_judet":         firma.get("adr_judet", ""),
                    "adr_localitate":    firma.get("adr_localitate", ""),
                    "adr_den_strada":        firma.get("adr_den_strada", ""),
                    "adr_nr_strada":         firma.get("adr_nr_strada", ""),
                    "adr_cod_postal":    firma.get("adr_cod_postal", ""),
                    "web":               firma.get("web", ""),
                    "tara_firma_mama":   firma.get("tara_firma_mama", ""),
                    "stare": stare_by_cod.get(cod_inm, []),
                }
            else:
                result["onrc_data"] = None

        return results


# ── Singleton ─────────────────────────────────────────────────────────────────
_svc: ONRCService | None = None


def get_onrc() -> ONRCService:
    global _svc
    if _svc is None:
        _svc = ONRCService()
    return _svc
