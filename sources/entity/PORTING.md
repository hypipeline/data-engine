# Entity Lookup — PHP → Python porting plan (strangler-fig)

`entity-lookup` currently runs as a **PHP sidecar** container inside Data Engine. The
Python API (`/entity/*`) and the dashboard Entity view proxy to it (see
`mergr_db/entity_client.py`). This lets the source be fully integrated *today* while we
port it to Python incrementally — behind a stable interface, with no big-bang rewrite.

## What it does
Given a company website URL, an LLM agent (Claude `claude-sonnet-4-6`, tool-calling)
extracts candidate legal names, verifies them in official registers/regulators, walks up
to the TopCo, builds a bidirectional evidence chain, and scores credit/substance
confidence — returning `null` rather than guessing. Output includes a prebuilt
`embed_html` report + `meta` (model, cost, tokens, timing). The JSON API is **async**:
`?format=json&url=` returns `202 {status:processing}` and must be polled until
`{status:complete}` (a lookup takes ~1–4 min; Apple ≈ 246s / $0.35).

## The pieces to port (from `php/`)
| PHP file | Responsibility | Python target |
|---|---|---|
| `lookup.php` | Orchestration (phases: fetch → extract → verify → topco → score) | `agent.py` |
| `tools.php` | LLM tool definitions + dispatch | `tools/__init__.py` |
| `oc.php` | OpenCorporates | `tools/opencorporates.py` |
| `bizapedia.php`, `bizapedia_tm.php` | Bizapedia (US) | `tools/bizapedia.py` |
| SEC (in lookup/tools) | EDGAR + IAPD | `tools/sec.py` |
| (Companies House API calls) | UK register | `tools/companies_house.py` |
| (North Data calls + login) | EU aggregator | `tools/northdata.py` |
| `browserbase_fetch.php`, `scraping_browser*.{php,mjs}` | headless fetch (Browserbase / Brightdata) | `tools/browser.py` — **reuse the existing Playwright setup from the Mergr scrapers** |
| `compare.php`, `validate.php` | name/registry validation | `verify.py` |
| `cache.php` | result cache | move to Postgres (`entity.lookups`) |
| `prompts/*.txt`, `prompts.php` | LLM prompts | reuse the `.txt` **verbatim** |
| `config.php`, `settings.json` | config/secrets | fold into shared Data Engine config |

## Order of work
0. **(done)** Sidecar wired in: `/entity/lookup` API proxy + dashboard view + cache volume.
1. Stand up `agent.py` (Anthropic Python SDK, tool-use loop) + prompt loading; stub tools.
2. Port tools **one at a time**, validating each against the existing PHP behaviour using
   the suite in `tests/` and `php/tests/` as an **oracle** (same URL in → same entity out).
   Start with SEC (highest signal), then Companies House, OpenCorporates, Bizapedia,
   North Data, WHOIS, then the headless browser fetch.
3. Move the cache to Postgres → unlocks a **lookup history** dashboard tab and lets Mergr
   records cross-link to resolved entities (enrich a Mergr company/firm with its legal
   contracting entity + credit score).
4. Flip `/entity/*` from proxy → native Python; retire the PHP sidecar (Phase 3).

## Notes
- Keep the **async poll contract** (`entity_client.lookup` already implements kick-off +
  poll); or move to a proper job table when native.
- `settings.json` holds live API keys (committed by decision) — centralise into shared config.
- The headless scraping connects to **remote** browsers (Browserbase/Brightdata), so no
  local Chromium is needed in the sidecar.
