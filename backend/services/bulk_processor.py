"""Bulk processor: async job with pause/resume/cancel/retry + offline mode."""
from __future__ import annotations
import asyncio, datetime, sqlite3, time

from backend.api.anaf_client import get_anaf_client, ANAFAPIError
from backend.services.normalizer import (
    normalize_company_info, normalize_financials, normalize_full,
    normalize_financials_from_db, normalize_company_from_onrc,
)
from backend.services.rate_limiter import get_bulk_limiter
from backend.jobs.job_manager import BulkJob
from backend.utils.logger import get_logger
from config import Config

log = get_logger(__name__)
BATCH      = 50
LOG_EVERY  = 500   # log a progress line every N companies (not every single one)


async def run_bulk_job(job: BulkJob):
    job.status     = "running"
    job.started_at = time.time()
    mode = "offline" if job.offline_financials else "online"
    job.add_log(f"▶ Started: {job.total} CUIs, years {job.years}, mode={mode}")

    if job.offline_financials:
        await _run_offline(job)
    else:
        await _run_online(job)

    # ONRC batch enrichment — single pass, includes stare data
    if job.onrc_enrich and job.results:
        try:
            from backend.services.onrc_service import get_onrc
            onrc = get_onrc()
            if onrc.is_indexed():
                _set_phase(job, "enriching", f"Enriching {len(job.results):,} results with ONRC registry data…")
                job.results = onrc.enrich_bulk_batch(job.results)
                enriched = sum(1 for r in job.results if r.get("onrc_data"))
                job.add_log(f"✓ ONRC enrichment: {enriched:,}/{len(job.results):,} companies")
            else:
                job.add_log("⚠ ONRC not indexed — skipping enrichment")
        except Exception as ex:
            log.warning("ONRC batch enrichment: %s", ex)
            job.add_log(f"⚠ ONRC enrichment error: {ex}")

    job.finished_at = time.time()
    _set_phase(job, "done", "")
    if job._cancel_flag:
        job.status = "cancelled"
        job.add_log(f"✖ Cancelled — {job.processed:,}/{job.total:,} processed")
    else:
        job.status = "done"
        elapsed = job.elapsed_seconds
        job.add_log(
            f"✅ Done — {job.success:,} OK · {job.failed:,} failed · "
            f"{elapsed:.0f}s ({elapsed/max(job.processed,1)*1000:.0f} ms/company)"
        )


# ── Online mode ───────────────────────────────────────────────────────

async def _run_online(job: BulkJob):
    client  = get_anaf_client()
    limiter = get_bulk_limiter()
    today   = datetime.date.today().strftime("%Y-%m-%d")
    cuis    = list(job.cuis)

    _set_phase(job, "company_info", f"Fetching identity data for {len(cuis):,} companies…")
    co_map: dict[int, dict | None] = {}
    for start in range(0, len(cuis), BATCH):
        if job._cancel_flag: return
        await job._pause_event.wait()
        batch = cuis[start: start + BATCH]
        try:
            await limiter.acquire()
            raw = await client.fetch_company_info(batch, today)
            for entry in raw.get("found") or []:
                dg = entry.get("date_generale") or {}
                c  = int(dg.get("cui", 0))
                if c: co_map[c] = normalize_company_info(entry)
            for c in raw.get("notFound") or []:
                co_map[int(c)] = None
        except ANAFAPIError as ex:
            job.add_log(f"⚠ Company-info batch {start//BATCH+1} error: {ex}")

    job.add_log(f"✓ Identity: {sum(1 for v in co_map.values() if v):,}/{len(cuis):,} found")

    _set_phase(job, "financials", f"Fetching financials ({len(job.years)} years × {len(cuis):,} companies)…")
    for cui in cuis:
        if job._cancel_flag: return
        await job._pause_event.wait()
        co   = co_map.get(cui)
        name = co.get("name", "") if co else ""
        job.current_cui  = cui
        job.current_name = name

        fins: dict[int, dict | None] = {}
        year_errs: list[str] = []
        for year in job.years:
            if job._cancel_flag: return
            await job._pause_event.wait()
            try:
                await limiter.acquire()
                fins[year] = normalize_financials(await client.fetch_financials(cui, year))
            except ANAFAPIError as ex:
                fins[year] = None; year_errs.append(f"{year}: {ex}")

        result = normalize_full(cui, co, fins, job.years)
        _record(job, result, name or f"CUI {cui}", fins, year_errs)

    job.current_cui  = None
    job.current_name = ""


# ── Offline mode ──────────────────────────────────────────────────────

async def _run_offline(job: BulkJob):
    cuis  = list(job.cuis)
    years = sorted(job.years)

    # Step 1: pre-fetch ALL financials from caen.db in one batch
    _set_phase(job, "loading_financials", f"Loading financials from caen.db for {len(cuis):,} companies…")
    fins_db = _fetch_all_financials(cuis, years)
    found_fin = sum(1 for v in fins_db.values() if any(fd for fd in v.values()))
    job.add_log(
        f"✓ Financials: {sum(len([fd for fd in v.values() if fd]) for v in fins_db.values()):,} "
        f"records for {found_fin:,}/{len(cuis):,} companies"
    )

    # Step 2: company metadata — ONRC first, ANAF fallback
    co_map: dict[int, dict | None] = {}
    try:
        from backend.services.onrc_service import get_onrc
        onrc = get_onrc()
        if onrc.is_indexed():
            _set_phase(job, "loading_meta", f"Loading company metadata from ONRC…")
            co_map = _fetch_company_meta_onrc(cuis, fins_db)
            found_onrc = sum(1 for v in co_map.values() if v)
            job.add_log(f"✓ ONRC metadata: {found_onrc:,}/{len(cuis):,} found")
    except Exception as ex:
        log.warning("ONRC metadata: %s", ex)

    missing = [c for c in cuis if not co_map.get(c)]
    if missing:
        _set_phase(job, "loading_meta_anaf", f"Fetching metadata for {len(missing):,} companies from ANAF…")
        client  = get_anaf_client()
        limiter = get_bulk_limiter()
        today   = datetime.date.today().strftime("%Y-%m-%d")
        anaf_found = 0
        for start in range(0, len(missing), BATCH):
            if job._cancel_flag: return
            batch = missing[start: start + BATCH]
            try:
                await limiter.acquire()
                raw = await client.fetch_company_info(batch, today)
                for entry in raw.get("found") or []:
                    dg = entry.get("date_generale") or {}
                    c  = int(dg.get("cui", 0))
                    if c:
                        co_map[c] = normalize_company_info(entry)
                        anaf_found += 1
                n_done = min(start + BATCH, len(missing))
                _set_phase(job, "loading_meta_anaf",
                    f"ANAF metadata: {n_done:,}/{len(missing):,} "
                    f"({anaf_found:,} found)…")
            except ANAFAPIError as ex:
                job.add_log(f"⚠ ANAF metadata batch error: {ex}")
        job.add_log(f"✓ ANAF metadata: {anaf_found:,}/{len(missing):,} found")

    # Step 3: build results (no API calls — just dict assembly)
    _set_phase(job, "processing", f"Building results for {len(cuis):,} companies…")
    t_start = time.time()
    for cui in cuis:
        if job._cancel_flag: return

        co   = co_map.get(cui)
        name = co.get("name", "") if co else ""
        job.current_cui  = cui
        job.current_name = name

        fins = fins_db.get(cui, {y: None for y in years})
        result = normalize_full(cui, co, fins, years)
        _record(job, result, name or f"CUI {cui}", fins, [])

        # Yield control + update phase ETA periodically
        if job.processed % 50 == 0:
            await asyncio.sleep(0)
        if job.processed % LOG_EVERY == 0 and job.processed > 0:
            elapsed = time.time() - t_start
            rate    = job.processed / elapsed if elapsed > 0 else 0
            remain  = job.total - job.processed
            eta_s   = int(remain / rate) if rate > 0 else 0
            eta_str = _fmt_eta(eta_s)
            _set_phase(job, "processing",
                f"Processing {job.processed:,}/{job.total:,} · "
                f"{rate:.0f}/s · ETA {eta_str}")

    job.current_cui  = None
    job.current_name = ""


# ── Helpers ───────────────────────────────────────────────────────────

def _set_phase(job: BulkJob, phase: str, message: str):
    """Update job phase/message — shows live in UI without flooding the log."""
    job.phase   = phase
    job.message = message


def _fmt_eta(seconds: int) -> str:
    if seconds >= 3600: return f"{seconds//3600}h {(seconds%3600)//60}m"
    if seconds >= 60:   return f"{seconds//60}m {seconds%60}s"
    return f"{seconds}s"


def _fetch_all_financials(cuis: list[int], years: list[int]) -> dict[int, dict[int, dict | None]]:
    result: dict[int, dict[int, dict | None]] = {c: {y: None for y in years} for c in cuis}
    if not Config.CAEN_DB_PATH.exists():
        return result
    conn = None
    CHUNK = 500
    try:
        conn = sqlite3.connect(str(Config.CAEN_DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        ph_years = ",".join("?" * len(years))
        for i in range(0, len(cuis), CHUNK):
            chunk    = cuis[i:i + CHUNK]
            ph_cuis  = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM caen_financials WHERE cui IN ({ph_cuis}) AND year IN ({ph_years})",
                (*chunk, *years)
            ).fetchall()
            for row in rows:
                cui  = row["cui"]
                year = row["year"]
                caen = row["caen"]
                if cui in result:
                    fin = normalize_financials_from_db(row, year, caen)
                    if fin:
                        result[cui][year] = fin
    except Exception as ex:
        log.warning("_fetch_all_financials: %s", ex)
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    return result


def _fetch_company_meta_onrc(cuis: list[int], fins_db: dict) -> dict[int, dict | None]:
    co_map: dict[int, dict | None] = {}
    if not Config.ONRC_DB_PATH.exists():
        return co_map
    conn = None
    CHUNK = 500
    try:
        conn = sqlite3.connect(str(Config.ONRC_DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        for i in range(0, len(cuis), CHUNK):
            chunk = cuis[i:i + CHUNK]
            ph = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""SELECT cui, cod_inmatriculare, denumire, forma_juridica,
                           data_inmatriculare, adr_judet, adr_localitate,
                           adr_den_strada, adr_nr_strada, adr_cod_postal, web, tara_firma_mama
                    FROM firme WHERE cui IN ({ph})""",
                chunk
            ).fetchall()
            for row in rows:
                cui = row["cui"]
                caen_from_db = 0
                if cui in fins_db:
                    for y_fin in sorted(fins_db[cui].keys(), reverse=True):
                        fd = fins_db[cui][y_fin]
                        if fd and fd.get("caen_code"):
                            try: caen_from_db = int(fd["caen_code"]); break
                            except: pass
                co_map[cui] = normalize_company_from_onrc(dict(row), caen_from_db)
    except Exception as ex:
        log.warning("_fetch_company_meta_onrc: %s", ex)
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    return co_map


def _record(job: BulkJob, result: dict, label: str, fins: dict, year_errs: list[str]):
    has_data = result.get("has_company_info") or result.get("has_financials")
    if has_data:
        job.results.append(result)
        job.success += 1
    else:
        job.failed += 1
        err = "; ".join(year_errs) if year_errs else "No data"
        job.errors.append({"cui": result.get("cui", ""), "name": label,
                           "error": err, "error_type": "no_data"})
        if job.failed <= 20 or job.failed % 100 == 0:
            job.add_log(f"  ✗ {label} — {err}")
    job.processed += 1


async def retry_failed(job: BulkJob):
    failed = [e["cui"] for e in job.errors]
    if not failed: return
    job.errors.clear()
    job.failed = 0
    old = job.cuis
    job.cuis  = failed
    job.total = len(failed)
    job.processed = 0
    job.status = "running"
    job._cancel_flag = False
    job._pause_event.set()
    job.add_log(f"↺ Retrying {len(failed):,} failed")
    await run_bulk_job(job)
    job.cuis = old
