# Mergr Relationship DB

Postgres + pgvector database modelling Mergr **companies**, **firms** (investors)
and **transactions**, with a Streamlit web UI and a gap-analysis queue for
records we don't yet hold.

## Layout

| File | Purpose |
|------|---------|
| `schema.sql` | Tables, indexes, pgvector columns, `v_party_resolution` view |
| `import.py` | Loads `mergr_investors/`, `mergr_companies/`, `mergr_transactions/` JSON (idempotent upsert) |
| `gap_analysis.py` | Writes `mergr_missing_companies.txt` / `mergr_missing_firms.txt` to the project root |
| `app.py` | Streamlit explorer (overview, firms, companies, transactions, vector search) |
| `docker-compose.yml` | `db` (pgvector), `loader`, `gap`, `web` services |

Data lives one level up in the project root (`../mergr_companies/` etc.) and is
mounted into the containers — nothing is copied.

## Data model

- `companies` — operating companies (`company_id` = mergr id)
- `firms` — investors (`firm_id` = mergr id)
- `transactions` — deals; `target_mergr_id` points at the target company
- `transaction_parties` — acquirers + sellers. **Polymorphic**: a party is a firm
  *or* a company, keyed by `(entity_type, entity_mergr_id)`. That's why parties
  have no hard FK — `gap_analysis.py` reconciles which referenced ids we're missing.

Embedding columns (`vector(1536)`, OpenAI `text-embedding-3-small`) exist on
`firms.criteria_embedding`, `companies.description_embedding`,
`transactions.target_embedding`, with HNSW cosine indexes ready. Switch to
`vector(3072)` for `text-embedding-3-large`.

## Run

```bash
cd mergr_db

# 1. start Postgres (schema auto-applies on first boot)
docker compose up -d db

# 2. load the scraped JSON (idempotent; ~400k files)
docker compose run --rm loader
#    quick smoke test first: set LIMIT in docker-compose.yml, or:
#    docker compose run --rm -e LIMIT=500 loader

# 3. build the missing-records queue files (written to project root)
docker compose run --rm gap

# 4. launch the web UI -> http://localhost:8501
docker compose up -d web
```

Postgres is exposed on **localhost:5433** (`mergr` / `mergr`, db `mergr`).

## Adding embeddings later

Backfill the vector columns (reuse the existing `backfill_embeddings.py` pattern),
set `OPENAI_API_KEY` in the environment, then the **Vector search** tab in the UI
turns a text query into an embedding and runs a pgvector cosine search.
