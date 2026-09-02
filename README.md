# ANAF Intelligence Platform v2.0

Romanian company financial analytics — fully local, no cloud, no auth.

---

## Quick Start

```bash
pip install -r requirements.txt
python main.py
```

Browser opens automatically at `http://127.0.0.1:8745`

---

## ANAF Features (always available)

- **Company Check** — single CUI lookup: full ANAF data + financials 2014–2024
- **Bulk Analysis** — upload a CSV of CUIs, process thousands automatically
- **Charts** — revenue trend, net result, profitability breakdown, sector/county distribution
- **Excel Export** — 4-sheet workbook: Results, Summary, Errors, Raw Indicators
- **Pause / Resume / Cancel / Retry** — full bulk job control

---

## ONRC Features (requires data files)

ONRC data enriches every lookup with:
- Full registry data (EUID, legal form, exact address, parent country, website)
- **Stare Firmă** — decoded company status (active, dissolved, suspended, etc.)
- **Reprezentanți Legali** — legal representatives with role, location, DOB
- **Name Search** — type a company name → instant autocomplete from local index

### Setup (one-time)

**Step 1 — Download from https://data.gov.ro/organization/onrc**

| File | Place in |
|------|----------|
| `OD_FIRME.csv` | `data/` folder |
| `OD_STARE_FIRMA.csv` | `data/` folder |
| `OD_REPREZENTANTI_LEGALI.csv` | `data/` folder |
| `nomenclator.csv` | project root |

> Files use `^` as delimiter (except nomenclator which uses `|`).
> Encoding: UTF-8 or CP1250 — handled automatically.

**Step 2 — Build the index**

When you launch the app with the files in place, the banner will show a **Build Index** button.
Click it. Indexing runs in the background (a few minutes for the full dataset).

The app is fully functional for ANAF lookups while indexing is in progress.

---

## Project Structure

```
main.py                    ← entry point
config.py                  ← all settings
requirements.txt
nomenclator.csv            ← place here (project root)
data/
  OD_FIRME.csv             ← place here
  OD_STARE_FIRMA.csv       ← place here
  OD_REPREZENTANTI_LEGALI.csv ← place here
  onrc.db                  ← auto-generated SQLite index
backend/
  api/anaf_client.py       ← ANAF HTTP client
  api/routes.py            ← all FastAPI endpoints
  services/onrc_service.py ← ONRC indexing + queries
  services/normalizer.py   ← ANAF JSON → clean dicts
  services/lookup_service.py
  services/bulk_processor.py
  services/analytics.py
  services/rate_limiter.py
  exporters/excel_exporter.py
  jobs/job_manager.py
  validators/validators.py
  utils/logger.py
frontend/index.html        ← complete SPA (no build step)
logs/anaf_intel.log
```

---

## Configuration (optional `.env`)

```
HOST=127.0.0.1
PORT=8745
REQUEST_TIMEOUT=20
BULK_RATE_DELAY=2.0
MAX_RETRIES=2
LOG_LEVEL=INFO
```

---

## ANAF API Notes

- Company info: `POST https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva`
- Financials: `GET https://webservicesp.anaf.ro/bilant?an={year}&cui={cui}`
- Rate limit: 1 req/sec — app uses 2s gap for safety
- Bulk batches company info 50 CUIs at a time; financials are fetched individually

---

## CSV Template (Bulk)

```csv
CUI
14399840
6480536
40066640
```

Download template: `http://127.0.0.1:8745/api/template`
