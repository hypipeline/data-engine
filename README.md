# Data Engine

A multi-source data API. Each data source is namespaced under its own prefix
(e.g. `/mergr/*`); new sources slot in as additional routers on the same app.

The first source is **Mergr** — a relationship database of private-equity firms,
companies, and M&A transactions (with financials), scraped from mergr.com,
stored in Postgres, and exposed via a JSON API plus a Streamlit explorer.

## Stack
- **Postgres + pgvector** — the data store (`mergr_db/schema.sql`)
- **FastAPI** — the JSON API (`mergr_db/api.py`), namespaced under `/mergr`, HTTP Basic auth
- **Streamlit** — the dashboard/explorer (`mergr_db/app.py`)
- **Docker Compose** — `db`, `api` (:8000), `web` (:8501), `loader` (`mergr_db/docker-compose.yml`)
- **Scrapers** (repo root, Playwright) — acquisition of the Mergr data:
  - `mergr_scrape_companies.py`, `mergr_scrape_investors.py`, `mergr_scrape_transactions.py`
  - `mergr_scrape_txn_details.py` (financials), `mergr_scrape_txn_parties.py` (acquirers/sellers)
  - `mergr_collect_*_ids.py` (listing enumeration), `mergr_fill_missing_txns.py`
  - `mergr_backfill_company_currency.py`, `mergr_parse_txn_detail.py`
- Shared helpers: `mergr_db/mergr_money.py` (currency formatting), `mergr_db/domain_utils.py`

## Run
```bash
cd mergr_db
docker compose up -d db        # Postgres (schema auto-applies)
docker compose run --rm loader # load scraped JSON into the DB
docker compose up -d api web   # API on :8000, dashboard on :8501
```
- **API docs:** http://localhost:8000/docs  ·  **Dashboard:** http://localhost:8501
- **Auth:** HTTP Basic (`API_USER`/`API_PASS` in `mergr_db/.env`)

## Data
The scraped JSON (~2.6 GB) and DB snapshots are **not** in the repo (`.gitignore`).
The database is fully rebuildable from the scraped files via the loaders, or from a
`pg_dump` snapshot.

## Currency
Every monetary value carries its own currency + scale; the API returns both the raw
value and a formatted string (`{amount, currency, scale, formatted, usd}`). USD
normalisation uses an editable `fx_rates` table (Settings tab in the dashboard).
