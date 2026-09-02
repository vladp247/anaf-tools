"""
Data Downloader Service
========================
Fetches ONRC registry CSVs and ANAF bulk financial-statement txt files
directly from data.gov.ro's CKAN API, so the setup wizard can offer a
one-click download instead of a manual browse-and-place workflow.

CKAN read-only endpoints need no auth:
  - package_search  → find datasets (used to locate the newest dated ONRC
                       "firme-DD-MM-YYYY" snapshot, since that slug changes
                       every time ONRC republishes)
  - package_show    → list a dataset's resources (name, download url, size)

Runs on a plain background thread (like onrc_service/caen_service's
indexers) with a synchronous httpx.Client — NOT the app's shared async
ANAF client — to avoid binding an async resource to a thread's own event
loop (see project bug history: threading.Thread + asyncio.run() crashes
when the resource was created on FastAPI's main loop).

Manually triggered only — never runs automatically on startup. Also does
NOT auto-trigger indexing when the download finishes; that stays a
separate, explicit "Build Index Now" step so the user chooses when the
(CPU/IO-heavy) indexing pass runs.
"""
from __future__ import annotations
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from config import Config
from backend.utils.logger import get_logger

log = get_logger(__name__)

CKAN_BASE = "https://data.gov.ro/api/3/action"

# ONRC files we actually use — resource basename (lowercased, no extension) → local filename.
# Deliberately excludes OD_CAEN_AUTORIZAT.CSV (deprecated data source, see bug history)
# and OD_SUCURSALE_ALTE_STATE_MEMBRE.CSV (unused).
ONRC_NEEDED = {
    "od_firme":                "OD_FIRME.CSV",
    "od_stare_firma":          "OD_STARE_FIRMA.CSV",
    "od_reprezentanti_legali": "OD_REPREZENTANTI_LEGALI.CSV",
    "od_reprezentanti_if":     "OD_REPREZENTANTI_IF.CSV",
}

# ── Global download state (polled by frontend, same pattern as the indexers) ──
_state: dict = {
    "status": "idle",   # idle | downloading | done | error
    "message": "",
    "error": None,
    "total_bytes": 0,
    "downloaded_bytes": 0,
    "files": {},         # key -> {label, size, downloaded, status}
}
_state_lock = threading.Lock()


def _upd(**kw):
    with _state_lock:
        _state.update(kw)


def _upd_file(key: str, **kw):
    with _state_lock:
        f = _state["files"].setdefault(key, {})
        f.update(kw)


def get_download_state() -> dict:
    with _state_lock:
        import copy
        return copy.deepcopy(_state)


# ── CKAN helpers ────────────────────────────────────────────────────────────

def _ckan_get(client: httpx.Client, action: str, **params) -> Any:
    r = client.get(f"{CKAN_BASE}/{action}", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"CKAN {action} failed: {data.get('error')}")
    return data["result"]


def find_latest_onrc_dataset(client: httpx.Client) -> str:
    """ONRC republishes the 'firme' dataset under a new dated slug each time
    (e.g. firme-08-07-2026), so we search rather than hardcode it."""
    result = _ckan_get(
        client, "package_search",
        q="name:firme-*", sort="metadata_modified desc", rows=5,
    )
    for pkg in result.get("results", []):
        if pkg["name"].startswith("firme-"):
            return pkg["name"]
    raise RuntimeError("Could not find a 'firme-*' dataset on data.gov.ro")


def get_onrc_resources(client: httpx.Client, dataset_name: str) -> dict[str, dict]:
    pkg = _ckan_get(client, "package_show", id=dataset_name)
    found: dict[str, dict] = {}
    for res in pkg.get("resources", []):
        base = res["name"].rsplit(".", 1)[0].lower()
        if base in ONRC_NEEDED:
            found[base] = {
                "label": ONRC_NEEDED[base],
                "url": res["url"],
                "size": res.get("size") or 0,
                "dest": Config.DATA_DIR / ONRC_NEEDED[base],
            }
    return found


def get_caen_resources(client: httpx.Client, year: int) -> dict[str, dict]:
    # data.gov.ro's dataset slugs aren't perfectly consistent year to year —
    # 2023's is "situatii_financiare2023" (no underscore) while every other
    # year is "situatii_financiare_{year}". Try both.
    pkg = None
    last_err: Exception | None = None
    for slug in (f"situatii_financiare_{year}", f"situatii_financiare{year}"):
        try:
            pkg = _ckan_get(client, "package_show", id=slug)
            break
        except Exception as ex:
            last_err = ex
    if pkg is None:
        raise RuntimeError(f"No situatii_financiare dataset found for {year}: {last_err}")

    wanted = {
        key: template.format(year=year)
        for key, template, desc, pri in Config.CAEN_FILE_DEFS
        if not Config.CAEN_KNOWN_MISSING.get((key, year))
    }
    found: dict[str, dict] = {}
    for res in pkg.get("resources", []):
        if (res.get("format") or "").upper() != "TXT":
            continue
        name_lower = res["name"].strip().lower()
        for key, filename in wanted.items():
            if key in found:
                continue
            fn_lower = filename.lower()
            # Some years drop the "_an" infix (e.g. 2024's WEB_IR_2024.txt
            # instead of WEB_IR_AN2024.txt) — accept that variant too.
            fn_alt = fn_lower.replace("_an", "_", 1)
            if name_lower in (fn_lower, fn_alt):
                found[f"{key}_{year}"] = {
                    "label": filename,
                    "url": res["url"],
                    "size": res.get("size") or 0,
                    "dest": Config.DATA_DIR / filename,
                }
    return found


# ── Download ─────────────────────────────────────────────────────────────────

def _download_one(client: httpx.Client, key: str, info: dict):
    dest: Path = info["dest"]
    tmp = dest.with_suffix(dest.suffix + ".part")
    downloaded = 0
    _upd_file(key, status="downloading", downloaded=0)
    try:
        with client.stream("GET", info["url"], timeout=httpx.Timeout(30, read=120)) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=1024 * 256):
                    f.write(chunk)
                    downloaded += len(chunk)
                    _upd_file(key, downloaded=downloaded)
                    with _state_lock:
                        _state["downloaded_bytes"] += len(chunk)
        tmp.replace(dest)
        _upd_file(key, status="done", downloaded=downloaded)
    except Exception as ex:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        _upd_file(key, status="error", error=str(ex))
        raise


def start_download(years: list[int], include_onrc: bool) -> bool:
    """Launch background download thread. Returns False if already running."""
    if get_download_state()["status"] == "downloading":
        return False
    t = threading.Thread(
        target=_worker, args=(years, include_onrc), daemon=True, name="data-downloader"
    )
    t.start()
    return True


def _worker(years: list[int], include_onrc: bool):
    _upd(status="downloading", message="Looking up latest data.gov.ro snapshots…",
         error=None, total_bytes=0, downloaded_bytes=0, files={})
    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(follow_redirects=True,
                           headers={"User-Agent": "ANAFIntelPlatform/2.0"}) as client:
            plan: list[tuple[str, dict]] = []

            if include_onrc:
                dataset = find_latest_onrc_dataset(client)
                _upd(message=f"Found ONRC snapshot: {dataset}")
                resources = get_onrc_resources(client, dataset)
                missing = set(ONRC_NEEDED) - set(resources)
                if missing:
                    log.warning("ONRC dataset %s missing resources: %s", dataset, missing)
                for key, info in resources.items():
                    plan.append((f"onrc_{key}", info))

            for year in years:
                try:
                    resources = get_caen_resources(client, year)
                except Exception as ex:
                    log.warning("CAEN dataset for %d unavailable: %s", year, ex)
                    continue
                for key, info in resources.items():
                    plan.append((f"caen_{key}", info))

            if not plan:
                _upd(status="error", error="No files found to download",
                     message="No files found to download")
                return

            total = sum(i["size"] for _, i in plan)
            with _state_lock:
                _state["files"] = {
                    k: {"label": i["label"], "size": i["size"], "downloaded": 0, "status": "pending"}
                    for k, i in plan
                }
            _upd(total_bytes=total, message=f"Downloading {len(plan)} file(s)…")

            for key, info in plan:
                _upd(message=f"Downloading {info['label']}…")
                _download_one(client, key, info)

        _upd(status="done", message="Download complete — build the index when you're ready")
        log.info("Data download complete: %d files", len(plan))

    except Exception as ex:
        log.error("Data download error: %s", ex, exc_info=True)
        _upd(status="error", error=str(ex), message=str(ex))
