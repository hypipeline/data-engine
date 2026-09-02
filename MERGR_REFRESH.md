# Running a Mergr refresh

How to pull everything Mergr has added since the last run, load it, and ship it to prod.

Last run: **28–29 Aug 2026** (import batch 2), from a 30 Jun 2026 baseline.
It produced +2,989 transactions, +2,259 companies, +38 PE firms and 29,641 recovered
acquirer links.

---

## Before you start

- **One Mergr login at a time.** Mergr permits a single session per account; starting a second
  login silently kicks out the running scrape, and every page then redirects to the login form.
  Check nothing else is logged in — including a browser tab you left open.
- **This runs on the Mac, not the EC2 box.** The box only serves the app; nothing Mergr-related
  runs there (no cron, no timers). Ingestion needs local Postgres on :5433, Playwright, and the
  scraped-data directories, none of which exist on the box.
- **Long jobs go under the relaunch wrapper**, which restarts on crash and wraps each attempt in
  `caffeinate` so the Mac does not sleep mid-run:

      ./run_until_clean.sh python3 <script>

- **Local state is gitignored and lives only on this machine**: `mergr_companies/`,
  `mergr_transactions/`, `mergr_txn_details/`, `mergr_investors/` (~2.6 GB), plus
  `mergr_txn_parties.jsonl`, `mergr_skip_ids.txt` and the `.state` files. Losing the big
  directories costs ~12 hours of re-scraping; losing the small ones costs a few hours. Back the
  small ones up before anything destructive.

---

## 1. Run the refresh

```bash
cd ~/Dropbox/dev/on-testing/data-engine
./run_until_clean.sh python3 mergr_sync.py --db mergr --years 3
```

`mergr_sync.py` is **deal-led** and loops until the gap stops shrinking:

1. re-crawls the last N years of transaction listings (default 3 — see *Why three years* below)
2. sweeps the firm id range (cheap, ~6 min, high signal)
3. loads the new JSON into Postgres
4. runs `gap_analysis.py` to find companies and firms the new deals reference but we do not hold
5. **scrapes detail pages for any deal with no acquirer** — mandatory, not optional (see *Traps*)
6. repeats from 3 until nothing new appears
7. validates, and **exits non-zero** if anything is wrong

### If validation fails

It prints `*** SYNC FAILED VALIDATION — do not ship this to prod` and lists what broke. Fix it
before shipping. The five invariants are: error-page records saved as companies, new deals still
lacking an acquirer, the same entity filed as both acquirer and seller, party rows pointing at a
transaction we do not hold, and companies with a blank name. Each exists because that exact
failure happened and was found by hand rather than by the pipeline.

### Why three years

Mergr adds deals retrospectively, months after announcement, so "everything since the last pull"
is not enough. Measured on the Aug 2026 run: of 2,989 deals collected, only **1,269 were newly
announced** — the other 1,720 were holes in coverage we already had, including 1,271 from 2025.
The yield falls off fast (2025: 1,271 · 2026 H1: 281 · 2024: 168), so three years is the agreed
trade-off. Going deeper is a job for an occasional full rebuild, not the routine refresh.

---

## 2. Ship it to prod

```bash
./sync_to_prod.sh mergr <import-date> <next-import-id> "what this batch contains"
# e.g. ./sync_to_prod.sh mergr 2026-08-28 3 "Sept refresh"
```

**Pass the date explicitly.** It is the `imported_at` date of the rows you are shipping, and it
is usually *not* today — a run that finishes after midnight, or a ship the next morning, will
select zero rows and report success. (`imported_at` is when WE imported the row, not the deal
date, so old backfilled deals ship correctly.)

**Pick the next unused import id.** Every row carries one: batch 1 is the pre-tagging baseline,
batch 2 was 28–29 Aug. `SELECT * FROM mergr_imports;` on prod shows what exists.

The script, in order: takes a rollback `pg_dump` of the four public Mergr tables → exports the
batch → saves every party row it is about to replace to a CSV inside the db container → loads,
tagging everything with the import id → prints totals → prints the other schemas to prove they
are untouched.

**It only ever touches `public.firms`, `public.companies`, `public.transactions` and
`public.transaction_parties`.** `buyer_match` (your live ON buyers, mandates and cached document
summaries), `entity` and `linkedin` live in the *same database*, so a dump/restore of the whole DB
would destroy them. That is why this is a row-scoped upsert and not a restore.

---

## 3. Check it landed

```bash
# counts must match local exactly
ssh -i ~/.ssh/data-engine-key.pem ec2-user@54.170.119.21 \
 "docker exec mergr_db-db-1 psql -U mergr -d mergr -tAF, -c \
  \"select (select count(*) from firms),(select count(*) from companies),
           (select count(*) from transactions),(select count(*) from transaction_parties);\""
```

Compare against the same query locally. **Counts matching is necessary but not sufficient** — the
right rows can be present in the wrong number, or the wrong rows in the right number. If you want
certainty, diff the id sets (a few MB of integers) rather than the totals.

Also confirm `buyer_match.buyers`, `buyer_match.mandates` and `buyer_match.doc_cache` have not
*dropped* (buyers grows on its own from the ON sync, so it may legitimately be higher).

### Then embed the new deals

New transactions arrive with a NULL embedding and are invisible to Buyer Match deal-history mode
until embedded:

```bash
ssh -i ~/.ssh/data-engine-key.pem ec2-user@54.170.119.21 \
 "cd ~/data-engine/mergr_db && docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec -T web python -m buyer_match.txn_embed"
```

Idempotent, embeds only what is missing. Cost is trivial — 2,784 deals cost $0.0041. Run it in
the **web** container: `api` has no OpenAI key.

---

## If it goes wrong

Every row of a batch is tagged, so a bad import is reversible without restoring the database:

```sql
DELETE FROM transaction_parties WHERE import_id = <N>;
\copy transaction_parties FROM '/tmp/import<N>_parties_replaced.csv' CSV   -- inside the db container
DELETE FROM transactions WHERE import_id = <N>;
DELETE FROM firms        WHERE import_id = <N>;   -- new rows only, see below
DELETE FROM companies    WHERE import_id = <N>;
```

Caveat: firms and companies that *already existed* are UPDATED by an import and their `import_id`
moves to the new batch, so deleting those would remove baseline records. For updated rows the undo
is the pre-import dump, kept on the box at `/tmp/mergr_pre_import<N>_<stamp>.dump`.

---

## Traps — do not "fix" these back out

Four defects were each losing data silently. The guards that stop them look like paranoia until
you know what they cost.

**1. Acquirer and seller are the LAST TWO cells, never fixed positions.**
A listing row that discloses a deal value renders extra `<td>`s, shifting both right. The old
parser read `tds[4]`/`tds[5]` and so, on exactly the rows big enough to publish a value, took
`"$1,500"` as the buyer and recorded no acquirer at all — **33,507 deals, 15% of the database**,
invisible to deal-history matching. Header-mapping does not fix this: the header row has one
layout and the body rows have several. Count from the right.

**2. A redirect to the login page is a dead SESSION, not a dead id.**
`mergr_scrape_companies.py` used to write every redirect to the permanent skip file, so one
expired session blacklisted every remaining id in the run, forever. It now returns `session-lost`
and halts, writing nothing. There is also a 600-consecutive-redirect circuit breaker — the largest
genuine gap ever measured in the id space is 172, so a long unbroken run means an outage.

**3. Only a `redirect` is permanent. Everything else must stay retryable.**
Mergr serves HTTP error pages whose `<title>` the parser reads as a company name, so
`"500 Internal Service Error"` was being saved as a real record — 269 of them in one run, which
also reset the frontier's dead-run counter and sent the walk 23,747 ids past the end. Similarly, an
*empty* detail-page scrape was recorded as fact, making 177 transient failures into permanent
"this deal has no parties" (txn 98288 was stored party-less while its live page listed Cambrex).
WAF blocks, 5xx pages, empty parses: park and retry, never skip-list.

**4. The skip file poisons future runs.**
An id that does not exist *yet* answers with a redirect and gets blacklisted before Mergr ever
creates it. Confirmed: 66 skip-listed ids around June's high-water mark were live and referenced by
deals crawled in August. Anything a deal references must be fetched with the skip file **ignored**:

    python3 mergr_delta.py recheck --ids-file <file>      # specific ids
    python3 mergr_delta.py recheck --range 340000-353225  # re-test a skip-listed band

**Open question:** the skip file holds ~153,000 ids and has never been swept. An unknown number are
live companies locked out by trap 2 before it was fixed. Sampling a band with `recheck --range`
would size it.

---

## Other tools

| Command | What it does |
|---|---|
| `python3 mergr_delta.py status` | on-disk counts, high-water marks, parked ids — no network, no login |
| `python3 mergr_delta.py probe` | find the live id frontier without writing anything |
| `python3 mergr_delta.py companies` | full company id-frontier walk — the occasional **annual rebuild**, not routine (it produced 2,193 companies of which only 345 appear in any deal) |
| `python3 mergr_delta.py firms` | firm id sweep — cheap and high-signal, part of every refresh |
| `python3 mergr_delta.py recheck` | re-test ids ignoring the skip file |
| `python3 mergr_db/load_txn_parties.py --only-ids F [--replace]` | load detail-page parties; `--replace` treats the detail page as authoritative over listing-derived rows |
| `python3 check_new_firms_vs_on.py out.txt` | which newly-found PE firms are absent from the **live** ON buyer list (not the replica, which lags) |

## Known state after the Aug 2026 run

- Acquirer coverage **96.0%** (99.95% of deals where an acquirer is obtainable at all). The
  residual 8,910 are accounted for: 4,521 structurally have none (IPO, bankruptcy, sold to
  management, shut down), 4,292 have only a seller on Mergr's own page, 97 have no linked entity.
- **564 deals** still list one entity as both acquirer and seller — needs detail data we do not
  hold for those deals.
- **498 transactions** remain unembedded (pre-existing, not from this batch).
- **10 company ids** parked as unresolved after repeated retries, in `mergr_delta.state`.
