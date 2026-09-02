"""
CAEN Analytics Service
======================
Indexes UU / BL_BS / IR txt files into caen.db SQLite.
Provides sector analytics with entity-type filtering:
  commercial | sucursala | nonprofit | public | other
"""
from __future__ import annotations
import csv, datetime, sqlite3, statistics, threading, time, uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from config import Config
from backend.utils.logger import get_logger

log = get_logger(__name__)

CAEN_YEARS = [2019, 2020, 2021, 2022, 2023, 2024, 2025]

SIZE_BUCKETS = [
    ("Micro",  0,          900_000),
    ("Small",  900_000,    8_800_000),
    ("Medium", 8_800_000,  88_000_000),
    ("Large",  88_000_000, float("inf")),
]

COUNTY_ABBR: dict[str, str] = {
    "alba":"AB","arad":"AR","argeș":"AG","arges":"AG","bacău":"BC","bacau":"BC",
    "bihor":"BH","bistrița-năsăud":"BN","bistrita-nasaud":"BN","bistrita nasaud":"BN",
    "botoșani":"BT","botosani":"BT","brașov":"BV","brasov":"BV","brăila":"BR","braila":"BR",
    "bucurești":"B","bucuresti":"B","municipiul bucurești":"B","buzău":"BZ","buzau":"BZ",
    "călărași":"CL","calarasi":"CL","caraș-severin":"CS","caras-severin":"CS",
    "caras severin":"CS","cluj":"CJ","constanța":"CT","constanta":"CT","covasna":"CV",
    "dâmbovița":"DB","dambovita":"DB","dolj":"DJ","galați":"GL","galati":"GL",
    "giurgiu":"GR","gorj":"GJ","harghita":"HR","hunedoara":"HD","ialomița":"IL",
    "ialomita":"IL","iași":"IS","iasi":"IS","ilfov":"IF","maramureș":"MM","maramures":"MM",
    "mehedinți":"MH","mehedinti":"MH","mureș":"MS","mures":"MS","neamț":"NT","neamt":"NT",
    "olt":"OT","prahova":"PH","satu mare":"SM","sălaj":"SJ","salaj":"SJ","sibiu":"SB",
    "suceava":"SV","teleorman":"TR","timiș":"TM","timis":"TM","tulcea":"TL","vaslui":"VS",
    "vâlcea":"VL","valcea":"VL","vrancea":"VN",
}

def _county_abbr(name: str) -> str:
    if not name: return ""
    key = name.lower().strip()
    if key in COUNTY_ABBR: return COUNTY_ABBR[key]
    if "sector" in key or key.startswith("bucurești") or key.startswith("bucuresti"): return "B"
    for k, v in COUNTY_ABBR.items():
        if k in key or key in k: return v
    return ""

# ── Entity type classification from FORMA_JURIDICA ────────────────────

def _norm(s: str) -> str:
    """Lowercase + strip diacritics for robust matching."""
    import unicodedata
    return unicodedata.normalize("NFD", s.lower()).encode("ascii", "ignore").decode()

# Exact abbreviations confirmed in OD_FIRME data:
# Commercial: SRL, SA, SNC, SCS, SCA, DP, SCR, PFA, PF, II, IF, AF, CA
# Sucursale:  anything containing SUCC (e.g. RA-SUCC, SRL-SUCC)
# Cooperative CA treated as commercial for financial analysis

_SUCURSALA_SUBSTR = ["succ", "sucursal", "branch", "reprezentant"]
_NONPROFIT_SUBSTR = ["asociati", "fundati", "federati", "ong", "cult",
                     "sindicat", "partid", "liga", "uniune"]
_PUBLIC_SUBSTR    = ["regie autonoma", "regia autonoma", "companie nationala",
                     "societate nationala", "institutie publica", "autoritate",
                     "minister", "prefectura", "primaria", "consiliu"]
# All confirmed commercial abbreviations from OD_FIRME
_COMMERCIAL_EXACT = {
    "srl", "sa", "sca", "snc", "scs", "pfa", "pf", "ii", "if",
    "af", "dp", "ra", "srl-d", "scr", "ca",
}

def classify_entity(forma_juridica: str) -> str:
    """Return one of: commercial | sucursala | nonprofit | public | other"""
    if not forma_juridica:
        return "other"
    fj = _norm(forma_juridica)

    # Sucursale first — contains SUCC regardless of prefix
    if any(t in fj for t in _SUCURSALA_SUBSTR):
        return "sucursala"

    # Nonprofit
    if any(t in fj for t in _NONPROFIT_SUBSTR):
        return "nonprofit"

    # Public
    if any(t in fj for t in _PUBLIC_SUBSTR):
        return "public"

    # Commercial — exact match on normalized first token
    base = fj.split()[0].rstrip("-") if fj.split() else fj
    if base in _COMMERCIAL_EXACT:
        return "commercial"

    # Fuzzy fallback for longer commercial forms
    if any(t in fj for t in ["srl", " sa ", "sca ", "snc ", "pfa", " ii ", " if "]):
        return "commercial"

    return "other"


# ── Global indexing state ─────────────────────────────────────────────
_idx_state: dict = {"status":"idle","phase":"","current":0,"total":0,"message":"","error":None}
_idx_lock  = threading.Lock()

def _upd(**kw):
    with _idx_lock: _idx_state.update(kw)

def get_index_state() -> dict:
    with _idx_lock: return dict(_idx_state)


# ── DB helpers ─────────────────────────────────────────────────────────

def _migrate_schema(conn: sqlite3.Connection):
    migrations = [
        "ALTER TABLE caen_financials ADD COLUMN source TEXT DEFAULT 'uu'",
        "ALTER TABLE caen_financials ADD COLUMN from_live_scan INTEGER DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_cf_source ON caen_financials(source)",
    ]
    for sql in migrations:
        try: conn.execute(sql)
        except Exception: pass
    conn.commit()


def _open_conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(Config.CAEN_DB_PATH), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    _migrate_schema(c)
    return c


class _ConnCtx:
    def __enter__(self) -> sqlite3.Connection:
        self._c = _open_conn(); return self._c
    def __exit__(self, *_):
        try: self._c.close()
        except Exception: pass

def _conn() -> _ConnCtx:
    return _ConnCtx()


# ── ONRC entity-type lookup helper ────────────────────────────────────

def _build_entity_map(cuis: list[int]) -> dict[int, str]:
    """Query onrc.db to get forma_juridica for a list of CUIs.
    Returns {cui: entity_type}. Empty dict if ONRC not indexed."""
    if not Config.ONRC_DB_PATH.exists() or not cuis:
        return {}
    entity_map: dict[int, str] = {}
    conn = None
    try:
        conn = sqlite3.connect(str(Config.ONRC_DB_PATH), timeout=20)
        for i in range(0, len(cuis), 500):
            batch = cuis[i:i+500]
            ph = ",".join("?" * len(batch))
            for row in conn.execute(
                f"SELECT cui, forma_juridica FROM firme WHERE cui IN ({ph})", batch
            ).fetchall():
                if row[0]:
                    entity_map[row[0]] = classify_entity(row[1] or "")
    except Exception as ex:
        log.debug("_build_entity_map: %s", ex)
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    return entity_map


# ══════════════════════════════════════════════════════════════════════
class CAENService:

    # ── File / DB status ─────────────────────────────────────────────

    def files_status_detail(self) -> dict:
        result: dict[int, dict] = {}
        for year in CAEN_YEARS:
            yr: dict[str, Any] = {}
            for key, template, desc, pri in Config.CAEN_FILE_DEFS:
                path = Config.DATA_DIR / template.format(year=year)
                km   = Config.CAEN_KNOWN_MISSING.get((key, year))
                yr[key] = {"present": path.exists(), "desc": desc,
                           "known_missing": km, "filename": template.format(year=year)}
            result[year] = yr
        return result

    def years_available(self) -> list[int]:
        avail = []
        for year in CAEN_YEARS:
            for key, template, *_ in Config.CAEN_FILE_DEFS:
                if (Config.DATA_DIR / template.format(year=year)).exists():
                    avail.append(year); break
        return avail

    def is_indexed(self) -> bool:
        if not Config.CAEN_DB_PATH.exists(): return False
        try:
            with _conn() as c:
                return c.execute("SELECT COUNT(*) FROM caen_financials").fetchone()[0] > 0
        except Exception: return False

    def db_stats(self) -> dict:
        if not self.is_indexed(): return {}
        try:
            with _conn() as c:
                return {
                    "rows":        c.execute("SELECT COUNT(*) FROM caen_financials").fetchone()[0],
                    "years":       c.execute("SELECT COUNT(DISTINCT year) FROM caen_financials").fetchone()[0],
                    "caen_codes":  c.execute("SELECT COUNT(DISTINCT caen) FROM caen_financials").fetchone()[0],
                    "companies":   c.execute("SELECT COUNT(DISTINCT cui) FROM caen_financials").fetchone()[0],
                    "source_uu":   c.execute("SELECT COUNT(*) FROM caen_financials WHERE source='uu'").fetchone()[0],
                    "source_bl":   c.execute("SELECT COUNT(*) FROM caen_financials WHERE source='bl_bs'").fetchone()[0],
                    "source_ir":   c.execute("SELECT COUNT(*) FROM caen_financials WHERE source='ir'").fetchone()[0],
                }
        except Exception: return {}

    # ── Indexing ─────────────────────────────────────────────────────

    def start_indexing(self) -> bool:
        if get_index_state()["status"] == "indexing": return False
        threading.Thread(target=self._worker, daemon=True, name="caen-indexer").start()
        return True

    def _worker(self):
        _upd(status="indexing", phase="init", current=0, total=0, message="Starting…", error=None)
        try:
            self._do_index()
        except Exception as ex:
            log.error("CAEN index error: %s", ex, exc_info=True)
            _upd(status="error", error=str(ex), message=str(ex))

    def _do_index(self):
        conn = sqlite3.connect(str(Config.CAEN_DB_PATH), timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=200000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.executescript("""
            DROP TABLE IF EXISTS caen_financials;
            DROP TABLE IF EXISTS caen_meta;
            DROP TABLE IF EXISTS caen_names;
        """)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS caen_financials (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                cui  INTEGER NOT NULL, caen INTEGER NOT NULL, year INTEGER NOT NULL,
                source TEXT DEFAULT 'uu', from_live_scan INTEGER DEFAULT 0,
                i1 REAL, i2 REAL, i3 REAL, i4 REAL, i5 REAL, i6 REAL,
                i7 REAL, i8 REAL, i9 REAL, i10 REAL, i11 REAL, i12 REAL,
                i13 REAL, i14 REAL, i15 REAL, i16 REAL, i17 REAL, i18 REAL,
                i19 REAL, i20 REAL, UNIQUE(cui, year)
            );
            CREATE INDEX IF NOT EXISTS idx_cf_caen_year ON caen_financials(caen, year);
            CREATE INDEX IF NOT EXISTS idx_cf_cui       ON caen_financials(cui);
            CREATE INDEX IF NOT EXISTS idx_cf_year      ON caen_financials(year);
            CREATE INDEX IF NOT EXISTS idx_cf_source    ON caen_financials(source);
            CREATE TABLE IF NOT EXISTS caen_meta (caen INTEGER PRIMARY KEY, description TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS caen_names (cui INTEGER PRIMARY KEY, company_name TEXT);
        """)
        conn.commit()

        file_jobs = sorted([
            (year, key, Config.DATA_DIR / template.format(year=year), pri)
            for year in CAEN_YEARS
            for key, template, desc, pri in Config.CAEN_FILE_DEFS
            if (Config.DATA_DIR / template.format(year=year)).exists()
            and not Config.CAEN_KNOWN_MISSING.get((key, year))
        ], key=lambda x: x[3])

        _upd(total=len(file_jobs))

        INSERT = (
            "INSERT OR REPLACE INTO caen_financials"
            "(cui,caen,year,source,from_live_scan,"
            "i1,i2,i3,i4,i5,i6,i7,i8,i9,i10,i11,i12,i13,i14,i15,i16,i17,i18,i19,i20)"
            " VALUES(?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )

        grand_total = 0
        for fi, (year, src_key, path, pri) in enumerate(file_jobs, 1):
            _upd(phase=f"{src_key}_{year}", current=fi-1,
                 message=f"Indexing {src_key} {year} ({fi}/{len(file_jobs)})…")
            log.info("CAEN: %s %d — %s", src_key, year, path.name)
            n = 0; batch = []
            for enc in ("utf-8-sig", "utf-8", "cp1250", "latin-1"):
                try:
                    with open(path, encoding=enc, newline="", errors="replace") as f:
                        reader = csv.reader(f)
                        next(reader, None)
                        for row in reader:
                            if len(row) < 2: continue
                            try:
                                cui  = int(row[0].strip()) if row[0].strip() else 0
                                caen = int(row[1].strip()) if row[1].strip() else 0
                                if cui <= 0 or caen <= 0: continue
                            except ValueError: continue
                            def _v(idx):
                                try: s=row[idx].strip() if idx<len(row) else ""; return float(s) if s else None
                                except: return None
                            batch.append((cui,caen,year,src_key,
                                _v(2),_v(3),_v(4),_v(5),_v(6),_v(7),_v(8),_v(9),_v(10),
                                _v(11),_v(12),_v(13),_v(14),_v(15),_v(16),_v(17),_v(18),
                                _v(19),_v(20),_v(21)))
                            n += 1
                            if len(batch) >= 10_000:
                                conn.executemany(INSERT, batch); conn.commit(); batch.clear()
                                _upd(message=f"{src_key} {year}: {n:,} rows…")
                        if batch: conn.executemany(INSERT, batch); conn.commit()
                        grand_total += n
                        log.info("CAEN %s %d: %d rows", src_key, year, n)
                    break
                except Exception as ex:
                    log.warning("CAEN enc %s failed for %s %d: %s", enc, src_key, year, ex)
                    batch.clear(); n = 0
            _upd(current=fi)

        conn.execute("INSERT OR IGNORE INTO caen_meta(caen,description) SELECT DISTINCT caen,'' FROM caen_financials")
        conn.commit()
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.close()
        _upd(status="done", current=len(file_jobs), total=len(file_jobs),
             message=f"Done — {grand_total:,} rows from {len(file_jobs)} files")

    # ── Query helpers ─────────────────────────────────────────────────

    def search_caen(self, q: str, limit: int = 20) -> list[dict]:
        if not self.is_indexed() or not q: return []
        try:
            with _conn() as c:
                if q.isdigit():
                    rows = c.execute("SELECT caen,description FROM caen_meta WHERE caen LIKE ? ORDER BY caen LIMIT ?", (q+"%",limit)).fetchall()
                else:
                    rows = c.execute("SELECT caen,description FROM caen_meta WHERE description LIKE ? ORDER BY caen LIMIT ?", (f"%{q}%",limit)).fetchall()
            return [{"caen":r["caen"],"description":r["description"]} for r in rows]
        except Exception as ex:
            log.warning("CAEN search: %s", ex); return []

    def cache_description(self, caen: int, desc: str):
        _desc_cache[caen] = desc
        try:
            with _conn() as c:
                c.execute("UPDATE caen_meta SET description=? WHERE caen=?", (desc, caen))
                c.connection.commit()
        except Exception: pass

    def get_description(self, caen: int) -> str:
        return _desc_cache.get(caen, "")

    def get_sample_cui(self, caen: int, year: int | None = None) -> int | None:
        try:
            with _conn() as c:
                q = "SELECT cui FROM caen_financials WHERE caen=? AND i13>0"
                params: list = [caen]
                if year: q += " AND year=?"; params.append(year)
                r = c.execute(q + " LIMIT 1", params).fetchone()
            return r["cui"] if r else None
        except Exception: return None

    def cache_company_name(self, cui: int, name: str):
        if not cui or not name: return
        try:
            with _conn() as c:
                c.execute("INSERT OR REPLACE INTO caen_names VALUES(?,?)", (cui, name))
                c.connection.commit()
        except Exception: pass

    # ── Entity enrichment ─────────────────────────────────────────────

    def _enrich_names(self, rows: list[dict]) -> list[dict]:
        if not rows: return rows
        cuis = [r["cui"] for r in rows]
        name_map: dict[int, str] = {}
        if Config.ONRC_DB_PATH.exists():
            conn = None
            try:
                conn = sqlite3.connect(str(Config.ONRC_DB_PATH), timeout=15)
                ph = ",".join("?" * len(cuis))
                for row in conn.execute(f"SELECT cui, denumire FROM firme WHERE cui IN ({ph})", cuis).fetchall():
                    if row[0] and row[1]: name_map[row[0]] = row[1].strip()
            except Exception as ex: log.debug("_enrich_names ONRC: %s", ex)
            finally:
                if conn:
                    try: conn.close()
                    except Exception: pass
        if len(name_map) < len(cuis):
            missing = [c for c in cuis if c not in name_map]
            try:
                with _conn() as c:
                    ph = ",".join("?" * len(missing))
                    for row in c.execute(f"SELECT cui, company_name FROM caen_names WHERE cui IN ({ph})", missing).fetchall():
                        if row[0] and row[1]: name_map[row[0]] = row[1]
            except Exception: pass
        for r in rows:
            r["name"] = name_map.get(r["cui"], f"CUI {r['cui']}")
        return rows

    def _county_distribution(self, cuis: list[int]) -> list[dict]:
        if not Config.ONRC_DB_PATH.exists() or not cuis: return []
        conn = None
        try:
            county_counts: dict[str, int] = {}
            conn = sqlite3.connect(str(Config.ONRC_DB_PATH), timeout=20)
            for i in range(0, len(cuis), 500):
                batch = cuis[i:i+500]
                ph = ",".join("?" * len(batch))
                for r in conn.execute(f"SELECT adr_judet, COUNT(*) FROM firme WHERE cui IN ({ph}) GROUP BY adr_judet", batch).fetchall():
                    county = (r[0] or "").strip()
                    if county:
                        abbr = _county_abbr(county) or county
                        county_counts[abbr] = county_counts.get(abbr, 0) + r[1]
            return [{"county":k,"count":v} for k,v in sorted(county_counts.items(),key=lambda x:-x[1]) if v>0]
        except Exception as ex: log.warning("County dist: %s", ex); return []
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    # ── Main analytics ────────────────────────────────────────────────

    def analyze(
        self,
        caen: int,
        years: list[int],
        anchor_year: int | None = None,
        peer_cui: int | None = None,
        entity_types: list[str] | None = None,  # None = all types
    ) -> dict:
        if not self.is_indexed() or not years:
            return {"empty": True, "reason": "Not indexed or no years selected"}

        yrs    = sorted(years)
        anchor = anchor_year if anchor_year in yrs else max(yrs)
        desc   = self.get_description(caen)

        # -- allowed entity types (default: commercial only)
        allowed = set(entity_types) if entity_types else {"commercial", "sucursala", "nonprofit", "public", "other"}

        try:
            with _conn() as c:
                ph   = ",".join("?" * len(yrs))
                rows = c.execute(f"""
                    SELECT cui, year, source,
                           COALESCE(i13,0) AS rev,
                           COALESCE(i18,0)-COALESCE(i19,0) AS net_result,
                           COALESCE(i20,0) AS employees,
                           COALESCE(i1,0)+COALESCE(i2,0) AS total_assets,
                           COALESCE(i10,0) AS equity,
                           COALESCE(i7,0) AS liabilities
                    FROM caen_financials
                    WHERE caen=? AND year IN ({ph})
                """, (caen, *yrs)).fetchall()

            if not rows:
                return {"empty": True, "reason": f"No data for CAEN {caen} in {yrs}"}

            # Build entity-type map for all distinct CUIs
            all_cuis = list({r["cui"] for r in rows})
            entity_map = _build_entity_map(all_cuis)
            onrc_available = bool(entity_map)

            # Group by CUI, apply entity filter
            companies: dict[int, dict] = defaultdict(dict)
            sources:   dict[int, str]  = {}
            entity_counts = {"commercial":0,"sucursala":0,"nonprofit":0,"public":0,"other":0}

            for r in rows:
                cui  = r["cui"]
                etype = entity_map.get(cui, "other")
                entity_counts[etype] = entity_counts.get(etype, 0) + 1
                if etype not in allowed:
                    continue
                companies[cui][r["year"]] = {
                    "rev": r["rev"] or 0, "net_result": r["net_result"] or 0,
                    "employees": r["employees"] or 0,
                    "total_assets": r["total_assets"] or 0,
                    "equity": r["equity"] or 0,
                    "liabilities": r["liabilities"] or 0,
                }
                sources[cui] = r["source"]

            # Anchor-year snapshot
            anchor_rows = [
                {"cui": cui, **fyears[anchor]}
                for cui, fyears in companies.items() if anchor in fyears
            ]
            if not anchor_rows:
                anchor_rows = [
                    {"cui": cui, **fyears[max(fyears.keys())]}
                    for cui, fyears in companies.items()
                ]

            all_rev = [r["rev"]        for r in anchor_rows if r["rev"] > 0]
            all_net = [r["net_result"] for r in anchor_rows]
            all_emp = [r["employees"]  for r in anchor_rows if r["employees"] > 0]

            def _s(lst, fn):
                try: return round(fn(lst), 2) if lst else None
                except: return None

            profitable  = sum(1 for r in anchor_rows if r["net_result"] > 0)
            loss_making = sum(1 for r in anchor_rows if r["net_result"] < 0)
            buckets = {nm: sum(1 for r in anchor_rows if lo <= r["rev"] < hi)
                       for nm, lo, hi in SIZE_BUCKETS}

            # CAGR
            cagr_revs, cagr_nets = [], []
            for cui, fyears in companies.items():
                rv = sorted(y for y in yrs if fyears.get(y,{}).get("rev",0) > 0)
                if len(rv) >= 2:
                    sy,ey,ny = rv[0],rv[-1],rv[-1]-rv[0]
                    try: cagr_revs.append(round(((fyears[ey]["rev"]/fyears[sy]["rev"])**(1/ny)-1)*100,2))
                    except: pass
                nt = sorted(y for y in yrs if fyears.get(y,{}).get("net_result",0) > 0)
                if len(nt) >= 2:
                    sy,ey,ny = nt[0],nt[-1],nt[-1]-nt[0]
                    try: cagr_nets.append(round(((fyears[ey]["net_result"]/fyears[sy]["net_result"])**(1/ny)-1)*100,2))
                    except: pass

            # Trend
            trend = []
            for y in yrs:
                y_rows = [{"rev":companies[cui].get(y,{}).get("rev",0),
                           "net":companies[cui].get(y,{}).get("net_result",0)}
                          for cui in companies if y in companies[cui]]
                y_revs = [r["rev"] for r in y_rows if r["rev"] > 0]
                trend.append({"year":y,"companies":len(y_rows),
                    "avg_rev":_s(y_revs,statistics.mean),
                    "median_rev":_s(y_revs,statistics.median),
                    "total_rev":round(sum(y_revs)) if y_revs else None,
                    "avg_net":_s([r["net"] for r in y_rows],statistics.mean),
                    "profitable":sum(1 for r in y_rows if r["net"]>0)})

            top10 = sorted(anchor_rows, key=lambda r: r["rev"], reverse=True)[:10]
            top10 = self._enrich_names(top10)
            # Tag entity type on top10
            for r in top10:
                r["entity_type"] = entity_map.get(r["cui"], "other")

            county = self._county_distribution(list(companies.keys()))

            pct = {}
            if all_rev:
                s = sorted(all_rev); n = len(s)
                for p in [10,25,50,75,90,95,99]:
                    pct[str(p)] = s[min(int(n*p/100),n-1)]

            # Peer benchmarking
            peer = None
            if peer_cui and peer_cui in companies:
                fy = companies[peer_cui]
                by = max(fy.keys())
                pr = fy[by]["rev"]; pn = fy[by]["net_result"]
                r_rv = sum(1 for r in anchor_rows if r["rev"] > pr)+1
                r_nt = sum(1 for r in anchor_rows if r["net_result"] > pn)+1
                tot  = len(anchor_rows)
                peer = {
                    "cui":peer_cui,"year":by,"revenue":pr,"net_result":pn,
                    "employees":fy[by]["employees"],
                    "rank_revenue":r_rv,"rank_net":r_nt,"total_companies":tot,
                    "pct_revenue":round((1-r_rv/tot)*100,1) if tot else 0,
                    "pct_net":round((1-r_nt/tot)*100,1) if tot else 0,
                    "revenue_vs_median":round(pr/_s(all_rev,statistics.median)*100-100,1)
                        if all_rev and _s(all_rev,statistics.median) else None,
                    "entity_type":entity_map.get(peer_cui,"other"),
                }
                rv = sorted(y for y in yrs if companies[peer_cui].get(y,{}).get("rev",0)>0)
                if len(rv)>=2:
                    sy,ey,ny=rv[0],rv[-1],rv[-1]-rv[0]
                    try: peer["cagr_revenue"]=round(((companies[peer_cui][ey]["rev"]/companies[peer_cui][sy]["rev"])**(1/ny)-1)*100,2)
                    except: pass

            src_counts = {"uu":0,"bl_bs":0,"ir":0}
            for cui in companies:
                s = sources.get(cui,"uu")
                if s in src_counts: src_counts[s]+=1

            return {
                "caen":caen,"description":desc,"anchor_year":anchor,"years":yrs,
                "total_companies":len(anchor_rows),"total_rows":len(rows),
                "entity_counts":entity_counts,       # all types before filter
                "entity_filter":list(allowed),       # what was applied
                "onrc_available":onrc_available,     # whether ONRC join worked
                "source_breakdown":src_counts,
                "revenue_stats":{"avg":_s(all_rev,statistics.mean),"median":_s(all_rev,statistics.median),
                    "total":round(sum(all_rev)) if all_rev else None,
                    "min":min(all_rev) if all_rev else None,"max":max(all_rev) if all_rev else None},
                "net_result_stats":{"avg":_s(all_net,statistics.mean),"median":_s(all_net,statistics.median),
                    "total":round(sum(all_net)) if all_net else None},
                "employee_stats":{"avg":_s(all_emp,statistics.mean),"median":_s(all_emp,statistics.median),
                    "total":int(sum(all_emp)) if all_emp else None},
                "profitability":{"profitable":profitable,"loss_making":loss_making,
                    "breakeven":len(anchor_rows)-profitable-loss_making,
                    "profitable_pct":round(profitable/len(anchor_rows)*100,1) if anchor_rows else 0},
                "size_buckets":buckets,
                "portfolio_cagr":{"revenue_median":_s(cagr_revs,statistics.median),
                    "revenue_avg":_s(cagr_revs,statistics.mean),
                    "net_median":_s(cagr_nets,statistics.median),
                    "companies":len(cagr_revs),"start_year":min(yrs),"end_year":max(yrs)},
                "trend":trend,"top10_revenue":top10,
                "county_distribution":county,"percentiles":pct,"peer":peer,
            }

        except Exception as ex:
            log.error("CAEN analyze: %s", ex, exc_info=True)
            return {"empty":True,"reason":str(ex)}

    def get_all_rows(self, caen: int, years: list[int], anchor_year: int | None = None,
                     entity_types: list[str] | None = None) -> list[dict]:
        """All companies for full Excel export — one row per company with
        financials for ALL requested years (not just anchor year)."""
        if not self.is_indexed() or not years: return []
        yrs    = sorted(years)
        anchor = anchor_year if anchor_year in yrs else max(yrs)
        allowed = set(entity_types) if entity_types else None
        try:
            with _conn() as c:
                ph   = ",".join("?" * len(yrs))
                rows = c.execute(f"""
                    SELECT cui, year, source,
                           COALESCE(i13,0) AS rev,
                           COALESCE(i18,0)-COALESCE(i19,0) AS net_result,
                           COALESCE(i20,0) AS employees,
                           COALESCE(i1,0)+COALESCE(i2,0) AS total_assets,
                           COALESCE(i10,0) AS equity,
                           COALESCE(i7,0)  AS liabilities,
                           COALESCE(i16,0)-COALESCE(i17,0) AS gross_result
                    FROM caen_financials
                    WHERE caen=? AND year IN ({ph})
                """, (caen, *yrs)).fetchall()

            # Group by CUI: {cui: {year: {...}}}
            by_cui: dict[int, dict] = {}
            sources: dict[int, str] = {}
            for r in rows:
                cui = r["cui"]
                if cui not in by_cui:
                    by_cui[cui] = {}
                by_cui[cui][r["year"]] = dict(r)
                sources[cui] = r["source"]

            all_cuis    = list(by_cui.keys())
            entity_map  = _build_entity_map(all_cuis)

            # Build one output row per company
            out = []
            for cui, yr_data in by_cui.items():
                etype = entity_map.get(cui, "other")
                if allowed and etype not in allowed: continue

                # Use anchor year for sorting/ranking
                anchor_fd = yr_data.get(anchor, {})
                anchor_rev = anchor_fd.get("rev", 0) or 0

                row = {
                    "cui":         cui,
                    "source":      sources.get(cui, "uu"),
                    "entity_type": etype,
                    "anchor_rev":  anchor_rev,   # for sorting
                    "years":       {},
                }
                # Attach per-year financials
                for y in yrs:
                    fd = yr_data.get(y)
                    row["years"][y] = {
                        "rev":          fd["rev"]          if fd else None,
                        "net_result":   fd["net_result"]   if fd else None,
                        "gross_result": fd["gross_result"] if fd else None,
                        "employees":    fd["employees"]    if fd else None,
                        "total_assets": fd["total_assets"] if fd else None,
                        "equity":       fd["equity"]       if fd else None,
                        "liabilities":  fd["liabilities"]  if fd else None,
                    }
                out.append(row)

            out = self._enrich_names(sorted(out, key=lambda r: r["anchor_rev"], reverse=True))
            return out
        except Exception as ex:
            log.error("get_all_rows: %s", ex, exc_info=True); return []


# ── Module-level cache + singleton ───────────────────────────────────
_desc_cache: dict[int, str] = {}
_svc: CAENService | None = None

def get_caen() -> CAENService:
    global _svc
    if _svc is None: _svc = CAENService()
    return _svc
