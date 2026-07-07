# Buyer Match — Data Engine integration (build spec)

Draft for review. Migrates the standalone `on-testing/match_server.py` (buyer-to-mandate
vector search, currently a stdlib HTTP server on :8889 with disk-based embeddings) into
**Data Engine** as a third tool alongside Mergr and Entity Lookup, storing everything in
Postgres + pgvector. **Goal: exact parity with the existing matching**, plus a Sync button.

---

## 1. Decisions locked (the "why" is in the chat)

| # | Decision |
|---|----------|
| 1 | Data Engine Postgres holds a **scoped replica** of the buyer/mandate slice. **Read-only prod `origryxd_main` is the upstream source of truth.** |
| 2 | Embeddings live **in-column** on the metadata tables, single **blended** vector each (`text-embedding-3-small`, 1536-d). Field-level embeddings deferred (cheap to add later via a typed `embeddings` table). |
| 3 | **Buyers + keywords are pre-embedded and stored** during Sync. **Mandates are metadata-only** in the replica — embedded **on-demand** at load time. |
| 4 | **Ranking = pure cosine** via pgvector (`ORDER BY embedding <=> :q`). Keyword lookups are separate side-features, not part of ranking. |
| 5 | Buyer re-embed decided by a **normalized text-hash + version salt** (authoritative); displayed metadata refreshes every sync. Mandates need no change-detection (not stored). |
| 6 | **Mandate on-demand pipeline reproduced exactly**: build text → fetch docs → parse (PDF/Excel) → gpt-4o-mini summarise → concatenate → embed. Docs *do* feed the ranking. |
| 7 | **Environments:** local → local Docker `origryxd_main` (:3307) + local backup disk for docs (no SSH). Prod → read-only prod DB via SSH tunnel + **SFTP** for docs. Source chosen by config. |
| 8 | **Manual Sync only** (no scheduler yet), with a **"records changed since last sync"** nudge (cheap `updated_at` count). |
| 9 | **No hardcoded OpenAI key** — use Data Engine's env `OPENAI_API_KEY`. (The key in `match_server.py`/`backfill_*.py`/`test_pillars.py` should be rotated — it's in plaintext.) |
| 10 | **No new always-on container.** Folds into the existing shared web/api image (it's a light Python app on the same Postgres — unlike `entity`, which is isolated for heavy browser deps). Sync runs as a worker (background job + standalone command), like the one-shot `loader`/`gap` tools. |

---

## 2. Architecture

**No new always-on container.** Buyer Match folds into the existing Data Engine Python
image (the one `web` and `api` already share) — it's a lightweight Python app on the same
Postgres, so a standalone service isn't justified (unlike `entity`, isolated for its heavy
browser deps). Two roles on that shared image:

- **Serving (in the `web` app):** the native **`/buyer-match`** page (light theme, `.tbl`
  conventions — no iframe) + hub card, and its JSON endpoints under **`/buyer-match/*`**,
  served same-origin by `web` (the Caddy catch-all — **no new Caddy route**), behind the one
  auth gate. This includes the **on-demand mandate pipeline** (doc fetch → parse →
  gpt-4o-mini → embed) and the pgvector search.
- **Sync (a worker):** a `buyer_match.sync` module runnable two ways from the same image —
  (a) triggered by the **Sync** button as an **in-process background job with SSE progress**
  (like the entity stream), and (b) as a **standalone command** (`python -m buyer_match.sync`)
  for CLI / future cron — same pattern as the one-shot `loader`/`gap` tools.

Deps added to the shared image (modest): `poppler`/`pdftotext` + PyPDF2 + openpyxl (doc
parsing — needed by the on-demand path too), `pymysql` (source reads, sync only), and an
SFTP client (`paramiko`) for prod docs. Postgres + OpenAI are already present.

Connections: **Postgres** (Data Engine) for the replica + vectors; **source MySQL**
(`origryxd_main`) only during sync; **documents** from local disk (dev) or SFTP (prod);
**OpenAI** for embeddings + gpt-4o-mini summaries (key from env).

```
Sync (worker):  origryxd_main (MySQL, ro) ──► sync ──► Postgres (buyers/keywords/mandates + vectors)
Search (web):   browser ──► /buyer-match ──► Postgres pgvector
Mandate load (on-demand, in web): docs (disk|SFTP) ──► parse ──► gpt-4o-mini ──► embed ──► pgvector rank
```

---

## 3. Postgres schema (`buyer_match`)

```sql
CREATE SCHEMA IF NOT EXISTS buyer_match;
CREATE EXTENSION IF NOT EXISTS vector;   -- already present (mergr uses pgvector)

-- Buyers: replica slice + blended embedding (stored)
CREATE TABLE buyer_match.buyers (
  id                   bigint PRIMARY KEY,        -- source buyers.id
  name                 text,
  description          text,
  investment_thesis    text,
  sector_keywords      text,
  website              text,
  tags                 text,                      -- comma-joined tag names (display + keyword features)
  email_count          int  DEFAULT 0,
  linkedin_count       int  DEFAULT 0,
  embedding            vector(1536),
  embed_model          text,
  embed_version        int  DEFAULT 1,            -- bump to force controlled re-embed
  embedding_text_hash  text,                      -- sha256("v{ver}:" + build_text)
  embedded_at          timestamptz,
  synced_at            timestamptz DEFAULT now()
);
CREATE INDEX buyers_embedding_hnsw ON buyer_match.buyers
  USING hnsw (embedding vector_cosine_ops);

-- Mandates: metadata only (embedded on-demand, NOT stored)
CREATE TABLE buyer_match.mandates (
  id                   bigint,
  source_table         text,                      -- 'opportunities' | 'bs_opportunities'
  code                 text,
  project_name         text,
  company_name         text,
  summary              text,
  points               jsonb,
  points_paragraph_top text,
  documents            jsonb,                     -- [{title, document(path)}]
  status               int,
  synced_at            timestamptz DEFAULT now(),
  PRIMARY KEY (source_table, id)
);
CREATE INDEX mandates_code ON buyer_match.mandates (code);

-- Keywords (+ embedding) for the similar-keywords feature
CREATE TABLE buyer_match.keywords (
  keyword      text PRIMARY KEY,
  embedding    vector(1536),
  embed_model  text,
  buyer_count  int DEFAULT 0,
  embedded_at  timestamptz
);
CREATE INDEX keywords_embedding_hnsw ON buyer_match.keywords
  USING hnsw (embedding vector_cosine_ops);

-- Sync bookkeeping (single row)
CREATE TABLE buyer_match.sync_state (
  id                     int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  last_sync_at           timestamptz,
  last_buyers_upserted   int,
  last_buyers_embedded   int,
  last_keywords_embedded int,
  last_mandates_synced   int,
  last_cost_usd          numeric
);
```

*(Optional later: memoise on-demand mandate embeddings by adding `embedding/doc_summaries/
source_hash` columns to `mandates` — "compute lazily, then cache". Off by default.)*

---

## 4. Exact parity recipes

**Buyer embedding text** (`backfill_embeddings.build_text`):
```
parts = [description, investment_thesis, sector_keywords]  # only non-empty
text  = "\n\n".join(parts)
```
Buyers considered: `deleted_at IS NULL AND (description OR investment_thesis OR sector_keywords non-empty)`.
Hash for change-detection = `sha256("v{embed_version}:" + text)`.

**Keyword set:** unique comma-split, trimmed values of `sector_keywords` across
`buyers WHERE deleted_at IS NULL`. Each embedded as the raw keyword string.

**Mandate query text** (`load_mandate` + frontend `fullText`), embedded on-demand:
```
mandate_text = "Summary: {summary}"
             + "\n{points_paragraph_top}"
             + "\n" + "\n".join("• "+p for p in points)      # points JSON
fullText = mandate_text
         + "\n\nDocument summaries:\n"
         + for each doc: "\n{title}:\n{summary}\n"
```
Document summary per file:
- **PDF:** `pdftotext -layout` (fallback PyPDF2) → truncate 8000 chars → gpt-4o-mini
  (prompt: *"Summarise this M&A document in 3-5 bullet points. Focus on: what the company
  does, key financials, sectors served, and any unique selling points. Be concise."*), 300 tok, temp 0.3.
- **Excel:** openpyxl (first 5 sheets × 50 rows) → truncate 8000 → gpt-4o-mini
  (prompt: *"Summarise this M&A financial data in 3-5 bullet points. Focus on: revenue,
  EBITDA, growth trends, and any notable metrics. Be concise."*), 300 tok, temp 0.3.

**Ranking:** embed the query, then
```sql
SELECT id, name, description, investment_thesis, sector_keywords, website, tags,
       email_count, linkedin_count,
       1 - (embedding <=> :qvec) AS score
FROM buyer_match.buyers
WHERE embedding IS NOT NULL
ORDER BY embedding <=> :qvec
LIMIT 500;
```
`<=>` = cosine distance → `score = 1 - distance` = cosine similarity (matches NumPy `matrix @ q / (norms·|q|)`).

> Honesty note: doc summaries are gpt-4o-mini @ temp 0.3 = non-deterministic, so mandate
> vectors aren't bit-identical run-to-run — already true of the current tool. Parity =
> same pipeline + inputs, rankings within that noise.

---

## 5. Sync job (manual, streaming progress)

`POST /buyer-match-app/sync` runs a background job (SSE progress log, like the entity
stream). Source = env-configured (local Docker MySQL or prod-ro tunnel). Steps:

1. **Buyers** — read the slice from source MySQL (`buyers` + `tags`/`buyer_tag` +
   `buyer_contacts` counts, deleted_at IS NULL). Upsert metadata into `buyer_match.buyers`.
   Compute `build_text` + hash; **re-embed only rows where hash changed or new** (batch
   100/OpenAI call). Delete rows gone from source / soft-deleted.
2. **Keywords** — rebuild the unique keyword set; embed any new keywords (batch 500);
   refresh `buyer_count`; drop keywords no longer present.
3. **Mandates** — upsert metadata from `opportunities` + `bs_opportunities` (no embedding).
4. Write `sync_state` (timestamp, counts, cost).

**"Records changed since" nudge** (`GET /buyer-match-app/sync-status`): cheap
`SELECT count(*) FROM buyers WHERE updated_at > :last_sync_at` (+ mandates) against source.
Rough gauge only; the sync itself stays hash-authoritative.

---

## 6. Endpoints (port of current server; served by the `web` app under `/buyer-match/*`)

| Method / path (under `/buyer-match`) | Purpose |
|---|---|
| `POST /search` `{query}` | embed query → pgvector top-500 → results + stats |
| `POST /load-mandate` `{identifier}` | on-demand: build fullText + doc summaries + gpt cost |
| `GET  /mandates` | dropdown list (from replica) |
| `GET  /keyword-counts` | keyword → buyer count |
| `POST /keyword-buyers` `{keywords}` | buyers matching keyword(s) |
| `POST /similar-keywords` `{keyword}` | keyword cosine neighbours (pgvector on `keywords`) |
| `POST /sync` | run sync (SSE progress) |
| `GET  /sync-status` | last sync + records-since counts |
| `GET  /health` | liveness |

Page: `GET /buyer-match` (the UI) + hub card — same `web` app. All of the above are one
shared image; only the sync worker runs as a separate invocation.

---

## 7. Config / secrets

- `OPENAI_API_KEY` — from env (Data Engine already injects it). **Remove hardcoded keys.**
- Source DB (sync): `BUYERMATCH_SOURCE_DSN` — local `mysql://root:…@origryxd-db:3307/origryxd_main`;
  prod = the read-only tunnel (`origryxd_readonly` via SSH-forwarded port).
- Docs: `BUYERMATCH_DOCS_MODE = local|sftp`; local → `DOCS_BASE` disk path; sftp → SSH creds
  (reuse `~/.ssh/cpanel_origryxd` on the box; base path `~/…/storage/app/public`).
- Postgres: the existing Data Engine `DATABASE_URL`.

Prod source access = the persistent read-only path from [[data-engine-aws-deploy]]: an
autossh tunnel on the box exposing prod MySQL (`origryxd_readonly`) as a local port the
web/api container reads during sync; SFTP over the same key (`~/.ssh/cpanel_origryxd`) for docs.

---

## 8. Build order (phased, each reviewable)

1. **Schema + buyer sync + backfill** — create `buyer_match.*`; sync job for buyers +
   keywords from local Docker source; one-shot backfill of all buyer/keyword embeddings
   into pgvector (retires the disk JSON). Verify counts vs the current tool (16,211 buyers).
2. **Search + keyword endpoints** — `/search`, `/keyword-*`, `/mandates` on pgvector.
   Verify a known mandate's top-N matches match the current tool.
3. **On-demand mandate pipeline** — `/load-mandate` with the exact doc recipe (local disk
   first; SFTP path stubbed). Verify fullText byte-parity (minus summary non-determinism).
4. **UI** — native `/buyer-match` page (port the HTML/JS to light theme + `.tbl`), hub card,
   Sync button + records-since.
5. **Prod wiring** — autossh tunnel + SFTP docs; prod sync (**no new container** — same image, sync worker command).
6. **Deploy** — ship like the rest (push to main).

---

## 9. Deferred / future (not in v1)

- Field-level embeddings (typed `embeddings` table + weighted scoring).
- Blended score (embedding + keyword overlap + structured region/industry/EBITDA).
- Scheduled/auto sync (cron) — currently manual.
- Lazy-cache of on-demand mandate embeddings.
- `mandate_matches` persistence + advisor review workflow (from the original design doc).
```
