# Entity Lookup — status & architecture

**The entity source is the original Python app** (`app/`), revived and integrated into
Data Engine. The `php/` dir is a later PHP *replica* that was tagged "legacy" in the
upstream repo and only ever pushed to GitHub because the Python was gitignored — it's kept
here as reference, **not deployed**.

## How it works (`app/`)
`server.py` (FastAPI) → `pipeline.lookup_entity()` gathers register data with fast HTTP
scrapers (WHOIS, website extraction, **SEC EDGAR**, **Companies House**, **North Data**),
then Claude (`claude-sonnet-4-6`) reasons over the gathered data into a structured report
(`viewer._render_report_card` renders it). It's *gather-then-reason*, not a tool-use loop.

**Routes:** `/` native form UI · `/lookup?url=` blocking HTML report · `/api/lookup` JSON
· **`/lookup/stream?url=`** SSE (live pipeline log) · **`/live?url=`** the "chatty" page
(URL form + streaming log + inline report) — this is what the dashboard embeds.

## Integration points
- Docker: `app/Dockerfile`; compose `entity` service (internal :8000, debug :9090).
- Keys: gitignored `mergr_db/entity.secrets.env` (ANTHROPIC, OPENAI, BROWSERBASE, CH).
- `mergr_db/entity_client.py` — client used by the API + dashboard.
- `/entity/lookup` API proxies it; dashboard Entity view iframes `/live`.

## Fixes applied during integration
- Retired model `claude-sonnet-4-20250514` → `claude-sonnet-4-6`.
- `max_tokens` 4096 → 8192 (reports were truncating → invalid JSON → "insufficient").
- Claude call moved off the event loop (`asyncio.to_thread`) so progress can stream.
- Added SSE streaming + `/live` page for the chatty UX.

## Known follow-ups
- **Browser register scrapers** (Delaware DOS, Ontario OBR, OpenCorporates — via
  Browserbase) are **opt-in** (`ENTITY_USE_BROWSER=1`): they currently hang/are slow and
  need per-scraper timeouts before enabling by default. The fast HTTP path (SEC + CH +
  North Data) covers US/UK/EU public + registered entities well.
- **No result caching** — every lookup re-runs (~$0.12, ~70s). Add a cache (Postgres
  `entity.lookups`) to reuse results + unlock a lookup-history view + Mergr enrichment.
- **Prod cutover (single-port monolith):** the whole app now runs behind ONE Caddy front
  door (committed `mergr_db/Caddyfile` + `docker-compose.yml`; prod domain/TLS/ports via
  `docker-compose.prod.yml`). api/web/entity publish no host ports; the entity app is only
  reachable at `/entity-app/*`. Cutover on the box, in order:
  1. `rm ~/data-engine/mergr_db/docker-compose.override.yml ~/data-engine/mergr_db/Caddyfile`
     (the old box-local files — they now conflict with the committed ones; git reset would fail).
  2. Ensure `~/data-engine/mergr_db/entity.secrets.env` exists (gitignored; ANTHROPIC/OPENAI/
     BROWSERBASE/CH keys).
  3. Merge branch → main. The deploy workflow now runs `docker compose -f docker-compose.yml
     -f docker-compose.prod.yml up` and reloads Caddy. Auth: `admin` / `silver-maple-harbor`.
