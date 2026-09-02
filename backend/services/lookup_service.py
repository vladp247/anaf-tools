"""Single-CUI lookup: ANAF company info + financials per year."""
from __future__ import annotations
import asyncio
import datetime

from backend.api.anaf_client import get_anaf_client, ANAFAPIError
from backend.services.normalizer import normalize_company_info, normalize_financials, normalize_full
from backend.utils.logger import get_logger

log = get_logger(__name__)


async def lookup_single(cui: int, years: list[int]) -> dict:
    client = get_anaf_client()
    today = datetime.date.today().strftime("%Y-%m-%d")
    errors: list[str] = []

    # Company info
    company = None
    try:
        raw = await client.fetch_company_info([cui], today)
        found = raw.get("found") or []
        if found:
            company = normalize_company_info(found[0])
            log.info("Company OK: CUI=%d → %s", cui, company.get("name", "?"))
        else:
            log.info("CUI=%d not found in ANAF", cui)
    except ANAFAPIError as ex:
        msg = f"Company info error: {ex}"
        log.warning("CUI=%d — %s", cui, msg)
        errors.append(msg)

    # Financials
    fins: dict[int, dict | None] = {}
    for year in sorted(years):
        try:
            raw_fin = await client.fetch_financials(cui, year)
            fins[year] = normalize_financials(raw_fin)
            await asyncio.sleep(0.5)
        except ANAFAPIError as ex:
            msg = f"Financials year={year}: {ex}"
            log.warning("CUI=%d — %s", cui, msg)
            errors.append(msg)
            fins[year] = None

    result = normalize_full(cui, company, fins, years)
    result["lookup_errors"] = errors
    return result
