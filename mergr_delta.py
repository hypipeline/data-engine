#!/usr/bin/env python3
"""
Mergr delta refresh — pull everything added since the last full scrape.

WHY THIS EXISTS
    The per-source scrapers are all resumable and skip-existing, but their ceilings are
    HARDCODED (`MAX_ID = 351000` in mergr_scrape_companies.py, `MAX_ID = 10000` in
    mergr_scrape_investors.py). Re-running them today processes 7 company ids and exits
    looking complete — every company added since 30 Jun 2026 has an id above the ceiling
    and is invisible. Silent truncation of exactly this kind cost us 47k transactions on
    the first transactions crawl. So this tool never trusts a constant: it walks the id
    frontier upward until the ids genuinely run out, and it SHOUTS if it stopped for any
    other reason.

FRONTIER WALK
    Mergr ids auto-increment, so everything new sits above our high-water mark. We walk
    upward in blocks, and stop only after DEAD_STOP consecutive dead ids. That threshold
    is derived from measured data, not guessed — the largest interior gap in the ids we
    already hold is 172 for companies and 11 for firms, so the thresholds below sit at
    ~6x and ~18x the worst real gap.

    Ids within each block are SHUFFLED before fetching. Strictly sequential access is what
    triggers the WAF rate limiter (48/min sequential vs 349/min shuffled, measured), and a
    frontier walk is sequential by nature — block-shuffling keeps the anti-WAF property.

CONSTRAINTS (learned the hard way — see the mergr-scraper / mergr-single-login memories)
    * ONE Mergr login at a time. Never run two of these concurrently, and nothing else
      logged in while it runs.
    * Only a `redirect` result is a permanently dead id. `waf` / `nodata` / errors are
      transient and MUST stay retryable — never write them to the skip file.
    * Long runs go under caffeinate + the relaunch wrapper: ./run_until_clean.sh

USAGE
    python3 mergr_delta.py status                 # on-disk + DB state, no network, no login
    python3 mergr_delta.py probe                  # find the live frontier, write nothing
    python3 mergr_delta.py companies [--limit N]  # scrape new companies above the mark
    python3 mergr_delta.py firms [--limit N]      # scrape new firms above the mark
    python3 mergr_delta.py txns                   # new transactions (widened years)

    --limit N bounds the walk to N candidate ids — use it for the gated test batch.
"""
import argparse
import asyncio
import json
import os
import random
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from mergr_scrape_companies import parse_company_page, EMAIL, PASSWORD   # noqa: E402
from mergr_scrape_investors import parse_firms_page, create_session, login as firm_login  # noqa: E402

COMPANY_DIR = os.path.join(BASE, "mergr_companies")
FIRM_DIR = os.path.join(BASE, "mergr_investors")
SKIP_FILE = os.path.join(BASE, "mergr_skip_ids.txt")
STATE_FILE = os.path.join(BASE, "mergr_delta.state")

# Frontier tuning. DEAD_STOP must exceed the largest real gap in the held id space
# (measured 28 Aug 2026: companies 172, firms 11) or the walk stops inside a gap and
# silently declares the frontier early — the exact failure this tool exists to prevent.
# Mergr intermittently serves an HTTP error PAGE with a 200-ish shell, whose <title> the
# parser happily reads as the company name ("500 Internal Service Error"). Those records
# look valid (name is truthy) so they were both WRITTEN to disk and counted as live ids,
# which reset the dead-run counter and made the frontier walk run 23,747 ids past the end.
# Treat them as transient errors: never write, never skip-list, always retry.
ERROR_PAGE_NAMES = {
    "500 internal service error", "500 internal server error", "502 bad gateway",
    "503 service unavailable", "504 gateway time-out", "504 gateway timeout",
    "404 not found", "403 forbidden", "access denied", "bad gateway",
    "javascript is disabled", "page not found", "too many requests",
}


def is_error_page(data):
    """True when the 'record' is really an error page wearing a company's id."""
    if not data:
        return True
    name = (data.get("name") or "").strip().lower()
    if name in ERROR_PAGE_NAMES:
        return True
    # A real listing always carries structure beyond a title. An error shell never does.
    return not any(data.get(k) for k in ("sector", "url", "description", "street", "city"))


COMPANY_BLOCK = 500
COMPANY_DEAD_STOP = 1000
COMPANY_PARALLEL = 10
FIRM_BLOCK = 200
FIRM_DEAD_STOP = 200
FIRM_DELAY_DEAD = 0.5
FIRM_DELAY_LIVE = 2.0


# --------------------------------------------------------------------------- state
def held_ids(directory):
    if not os.path.isdir(directory):
        return set()
    out = set()
    for f in os.listdir(directory):
        if f.endswith(".json"):
            try:
                out.add(int(f[:-5]))
            except ValueError:
                pass
    return out


def skip_ids():
    out = set()
    if os.path.exists(SKIP_FILE):
        for line in open(SKIP_FILE):
            line = line.strip()
            if line.isdigit():
                out.add(int(line))
    return out


def append_skip(ids):
    """Only ever called with `redirect` ids — a permanently non-existent record."""
    if not ids:
        return
    with open(SKIP_FILE, "a") as f:
        for i in sorted(ids):
            f.write(f"{i}\n")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(d):
    json.dump(d, open(STATE_FILE, "w"), indent=1)


# --------------------------------------------------------------------------- companies
async def _company_worker(ctx, queue, results):
    pg = await ctx.new_page()
    try:
        while True:
            try:
                cid = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            url = f"https://mergr.com/company/{cid}"
            try:
                await pg.goto(url, wait_until="domcontentloaded", timeout=30000)
                if pg.url != url and "/company/" not in pg.url:
                    results[cid] = ("redirect", None)
                    continue
                html = await pg.content()
                if "awswaf" in html:                       # WAF: back off, never skip-list
                    for _ in range(6):
                        await asyncio.sleep(5)
                        html = await pg.content()
                        if "awswaf" not in html:
                            break
                    else:
                        results[cid] = ("waf", None)
                        continue
                data = parse_company_page(html, cid)
                if data and data.get("name") and is_error_page(data):
                    results[cid] = ("errorpage", None)      # transient — retry, never write
                elif data and data.get("name"):
                    results[cid] = ("ok", data)
                else:
                    results[cid] = ("nodata", None)
            except Exception as e:                          # noqa: BLE001 — transient, retryable
                results[cid] = (f"error:{type(e).__name__}", None)
    finally:
        await pg.close()


async def walk_companies(limit=None, dry_run=False, retry_only=False):
    from playwright.async_api import async_playwright

    have, dead = held_ids(COMPANY_DIR), skip_ids()
    # Transient failures (waf/nodata/error) sit BELOW the advancing high-water mark, so a
    # naive frontier walk would step past them and never look again — silent under-collection.
    # They are parked in the state file and retried at the head of the next run.
    pending = [i for i in load_state().get("companies_pending", []) if i not in have and i not in dead]
    start = max(have) + 1 if have else 1
    if pending:
        print(f"companies: retrying {len(pending)} unresolved id(s) parked by an earlier run")
    if retry_only:
        # Frontier already confirmed — just resolve the parked ids, don't re-walk the tail.
        if not pending:
            print("companies: nothing parked, nothing to do")
            return [], {}, "retry-only"
        print("companies: RETRY-ONLY — the frontier walk is skipped")
    print(f"companies: {len(have):,} held, high-water mark {max(have):,}; walking from {start:,}")
    if dry_run:
        print("PROBE MODE — nothing will be written to disk")

    stats = {"ok": 0, "redirect": 0, "waf": 0, "nodata": 0, "error": 0, "errorpage": 0, "checked": 0}
    new_ids, consecutive_dead, cid, stopped = [], 0, start, "frontier"
    unresolved = set()
    errorpage_run = 0                                       # consecutive blocks dominated by 5xx

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()

        async def block(route):                             # ids live in the initial HTML
            if route.request.resource_type in ("image", "stylesheet", "font", "media"):
                await route.abort()
            else:
                await route.continue_()
        await ctx.route("**/*", block)

        lg = await ctx.new_page()                           # single login, shared context
        await lg.goto("https://mergr.com/login", wait_until="domcontentloaded", timeout=60000)
        await lg.fill('input[name="username"]', EMAIL)   # NOT "email" — the field is username
        await lg.fill('input[name="password"]', PASSWORD)
        await lg.click('button[type="submit"]')
        await lg.wait_for_url("**/dashboard**", timeout=20000)
        await lg.close()
        await asyncio.sleep(3)                              # settle, as the main scraper does
        print("logged in (single session — nothing else may be logged in)")

        try:
            while consecutive_dead < COMPANY_DEAD_STOP:
                if retry_only and not pending:
                    stopped = "retry-only"
                    break
                if limit and stats["checked"] >= limit:
                    stopped = "limit"
                    break
                candidates = []
                while pending and len(candidates) < COMPANY_BLOCK:   # retries go first
                    candidates.append(pending.pop())
                while not retry_only and len(candidates) < COMPANY_BLOCK:
                    if cid not in have and cid not in dead:
                        candidates.append(cid)
                    cid += 1
                    if limit and stats["checked"] + len(candidates) >= limit:
                        break
                random.shuffle(candidates)                  # never hit ids in sequence

                queue = asyncio.Queue()
                for c in candidates:
                    queue.put_nowait(c)
                results = {}
                await asyncio.gather(*[_company_worker(ctx, queue, results)
                                       for _ in range(COMPANY_PARALLEL)])

                block_dead, block_new = [], 0
                for c in sorted(results):
                    status, data = results[c]
                    stats["checked"] += 1
                    key = status.split(":")[0]
                    stats[key] = stats.get(key, 0) + 1
                    if status == "ok":
                        block_new += 1
                        new_ids.append(c)
                        if not dry_run:
                            json.dump(data, open(os.path.join(COMPANY_DIR, f"{c}.json"), "w"))
                    elif status == "redirect":
                        block_dead.append(c)
                    else:
                        unresolved.add(c)          # waf / nodata / error / 5xx — retry next run
                if not dry_run:
                    append_skip(block_dead)                 # redirect only — never waf/nodata
                # An error-page storm means the SITE is unhappy, not that the data ended.
                # Back off hard rather than burning through the id space misreading 5xx as data.
                block_err = sum(1 for c in results if results[c][0] == "errorpage")
                if block_err > len(results) * 0.5:
                    errorpage_run += 1
                    wait = min(60 * errorpage_run, 300)
                    print(f"  !! {block_err}/{len(results)} error pages — backing off {wait}s "
                          f"(storm #{errorpage_run})")
                    if errorpage_run >= 3:
                        stopped = "error-page storm"
                        break
                    await asyncio.sleep(wait)
                else:
                    errorpage_run = 0
                consecutive_dead = consecutive_dead + len(block_dead) if block_new == 0 else 0
                print(f"  .. up to {cid:,}: +{block_new} new, {len(block_dead)} dead "
                      f"(run of {consecutive_dead} dead) | total new {len(new_ids)}")
        finally:
            await browser.close()

    if not dry_run:
        st = load_state()
        st["companies_pending"] = sorted(unresolved)
        save_state(st)
    _report("companies", stats, new_ids, stopped, cid, len(unresolved))
    return new_ids, stats, stopped


# --------------------------------------------------------------------------- firms
def walk_firms(limit=None, dry_run=False):
    """Firms use plain requests — invalid ids answer 302, which is far cheaper than a
    browser nav, so no Playwright here (matches mergr_scrape_investors.py)."""
    have = held_ids(FIRM_DIR)
    pending = [i for i in load_state().get("firms_pending", []) if i not in have]
    start = max(have) + 1 if have else 2
    if pending:
        print(f"firms: retrying {len(pending)} unresolved id(s) parked by an earlier run")
    print(f"firms: {len(have):,} held, high-water mark {max(have):,}; walking from {start:,}")
    if dry_run:
        print("PROBE MODE — nothing will be written to disk")

    session = create_session()
    if not firm_login(session):
        print("FIRM LOGIN FAILED — aborting")
        return [], {}, "login-failed"
    time.sleep(3)

    stats = {"ok": 0, "redirect": 0, "nodata": 0, "error": 0, "checked": 0}
    new_ids, consecutive_dead, fid, stopped = [], 0, start, "frontier"
    unresolved = set()

    while consecutive_dead < FIRM_DEAD_STOP:
        if limit and stats["checked"] >= limit:
            stopped = "limit"
            break
        candidates = []
        while pending and len(candidates) < FIRM_BLOCK:              # retries go first
            candidates.append(pending.pop())
        while len(candidates) < FIRM_BLOCK:
            if fid not in have:
                candidates.append(fid)
            fid += 1
            if limit and stats["checked"] + len(candidates) >= limit:
                break
        random.shuffle(candidates)

        block_new = 0
        for f in candidates:
            stats["checked"] += 1
            try:
                r = session.get(f"https://mergr.com/firms/{f}", allow_redirects=False, timeout=30)
                if r.status_code == 302:
                    stats["redirect"] += 1
                    consecutive_dead += 1
                    time.sleep(FIRM_DELAY_DEAD)
                    continue
                if r.status_code == 200:
                    time.sleep(FIRM_DELAY_LIVE)
                    r = session.get(f"https://mergr.com/firms/{f}", timeout=30)
                    data = parse_firms_page(r.text, f)
                    if data and data.get("name"):
                        stats["ok"] += 1
                        block_new += 1
                        consecutive_dead = 0
                        new_ids.append(f)
                        if not dry_run:
                            json.dump(data, open(os.path.join(FIRM_DIR, f"{f}.json"), "w"))
                    else:
                        stats["nodata"] += 1                # transient — stays retryable
                        unresolved.add(f)
                else:
                    stats["error"] += 1
                    unresolved.add(f)
            except Exception as e:                          # noqa: BLE001
                stats["error"] += 1
                unresolved.add(f)
                print(f"  [firm {f}] {type(e).__name__}: {e}")
                time.sleep(2)
        print(f"  .. up to {fid:,}: +{block_new} new (run of {consecutive_dead} dead) "
              f"| total new {len(new_ids)}")

    if not dry_run:
        st = load_state()
        st["firms_pending"] = sorted(unresolved)
        save_state(st)
    _report("firms", stats, new_ids, stopped, fid, len(unresolved))
    return new_ids, stats, stopped


# --------------------------------------------------------------------------- reporting
def _report(label, stats, new_ids, stopped, reached, unresolved=0):
    print(f"\n=== {label}: {len(new_ids)} new record(s) ===")
    print("   " + "  ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    print(f"   walked up to id {reached:,}")
    if stopped == "frontier":
        print("   STOP REASON: frontier reached (a full dead-run) — coverage is complete.")
    else:
        print(f"   *** STOP REASON: {stopped} — coverage is NOT complete. Re-run without")
        print("   *** the bound to finish, or the delta silently under-collects.")
    if unresolved:
        print(f"   {unresolved} id(s) parked as unresolved (transient) — retried first next run")
    if new_ids:
        lo, hi = min(new_ids), max(new_ids)
        print(f"   new id range: {lo:,}..{hi:,}")


async def recheck(ids):
    """Re-fetch specific ids IGNORING the skip file, and un-skip any that prove live.

    Why this is needed: an id that does not exist YET answers with a redirect, which the
    scrapers treat as permanently dead and write to the skip file. When Mergr later creates
    that record the id is already blacklisted, so no delta will ever fetch it — silent,
    permanent under-collection in exactly the band where new records appear. Confirmed on
    28 Aug 2026: 66 skip-listed ids around June's high-water mark are referenced by deals
    crawled today, i.e. they exist now.
    """
    from playwright.async_api import async_playwright
    have = held_ids(COMPANY_DIR)
    todo = [i for i in ids if i not in have]
    print(f"recheck: {len(todo)} id(s), skip file deliberately ignored")
    live, still_dead = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()

        async def block(route):
            if route.request.resource_type in ("image", "stylesheet", "font", "media"):
                await route.abort()
            else:
                await route.continue_()
        await ctx.route("**/*", block)
        lg = await ctx.new_page()
        await lg.goto("https://mergr.com/login", wait_until="domcontentloaded", timeout=60000)
        await lg.fill('input[name="username"]', EMAIL)
        await lg.fill('input[name="password"]', PASSWORD)
        await lg.click('button[type="submit"]')
        await lg.wait_for_url("**/dashboard**", timeout=20000)
        await lg.close()
        await asyncio.sleep(3)

        random.shuffle(todo)
        queue = asyncio.Queue()
        for c in todo:
            queue.put_nowait(c)
        results = {}
        await asyncio.gather(*[_company_worker(ctx, queue, results)
                               for _ in range(COMPANY_PARALLEL)])
        await browser.close()

    for c, (status_, data) in results.items():
        if status_ == "ok":
            live.append(c)
            json.dump(data, open(os.path.join(COMPANY_DIR, f"{c}.json"), "w"))
        elif status_ == "redirect":
            still_dead.append(c)
    if live:                                    # purge the proven-live ids from the skip file
        dead = skip_ids() - set(live)
        with open(SKIP_FILE, "w") as f:
            for i in sorted(dead):
                f.write(f"{i}\n")
    print(f"recheck: {len(live)} were ALIVE (now scraped, un-skipped), {len(still_dead)} still dead, "
          f"{len(results) - len(live) - len(still_dead)} unresolved")
    return live


def recheck_firms(ids):
    """Fetch specific firm ids (those a new deal references but we do not hold)."""
    have = held_ids(FIRM_DIR)
    todo = [i for i in ids if i not in have]
    print(f"recheck-firms: {len(todo)} id(s)")
    if not todo:
        return []
    session = create_session()
    if not firm_login(session):
        print("FIRM LOGIN FAILED")
        return []
    time.sleep(3)
    live, dead, unresolved = [], [], []
    for f in todo:
        try:
            r = session.get(f"https://mergr.com/firms/{f}", allow_redirects=False, timeout=30)
            if r.status_code == 302:
                dead.append(f)
                time.sleep(FIRM_DELAY_DEAD)
                continue
            time.sleep(FIRM_DELAY_LIVE)
            r = session.get(f"https://mergr.com/firms/{f}", timeout=30)
            data = parse_firms_page(r.text, f)
            if data and data.get("name"):
                json.dump(data, open(os.path.join(FIRM_DIR, f"{f}.json"), "w"))
                live.append(f)
            else:
                unresolved.append(f)
        except Exception as e:                              # noqa: BLE001
            print(f"  [firm {f}] {type(e).__name__}: {e}")
            unresolved.append(f)
    print(f"recheck-firms: {len(live)} scraped, {len(dead)} dead (302), {len(unresolved)} unresolved")
    return live


def status():
    comp, firm = held_ids(COMPANY_DIR), held_ids(FIRM_DIR)
    txn = len(os.listdir(os.path.join(BASE, "mergr_transactions"))) \
        if os.path.isdir(os.path.join(BASE, "mergr_transactions")) else 0
    print(f"companies : {len(comp):,} held, max id {max(comp):,}")
    print(f"firms     : {len(firm):,} held, max id {max(firm):,}")
    print(f"txn files : {txn:,}")
    print(f"skip ids  : {len(skip_ids()):,}")
    st = load_state()
    for k in ("companies_pending", "firms_pending"):
        if st.get(k):
            print(f"{k}: {len(st[k])} id(s) awaiting retry")
    if st:
        print(f"delta state: { {k: v for k, v in st.items() if not k.endswith('_pending')} }")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "probe", "companies", "firms", "txns", "recheck", "recheck-firms"])
    ap.add_argument("--ids-file", help="recheck: file of ids, one per line")
    ap.add_argument("--range", help="recheck: inclusive id range, e.g. 340000-353225")
    ap.add_argument("--limit", type=int, help="bound the walk to N candidate ids (test batches)")
    ap.add_argument("--only", choices=["companies", "firms"], help="probe just one source")
    ap.add_argument("--retry-only", action="store_true",
                    help="resolve parked (transient-failure) ids without re-walking the frontier")
    args = ap.parse_args()

    if args.command == "recheck-firms":
        ids = [int(l) for l in open(args.ids_file) if l.strip().isdigit()] if args.ids_file else []
        if not ids:
            print("recheck-firms needs --ids-file")
            return
        recheck_firms(sorted(set(ids)))
        return
    if args.command == "recheck":
        ids = []
        if args.ids_file:
            ids += [int(l) for l in open(args.ids_file) if l.strip().isdigit()]
        if args.range:
            lo, hi = (int(x) for x in args.range.split("-"))
            ids += [i for i in range(lo, hi + 1) if i in skip_ids()]
        if not ids:
            print("recheck needs --ids-file and/or --range")
            return
        asyncio.run(recheck(sorted(set(ids))))
        return
    if args.command == "status":
        status()
        return
    if args.command == "probe":
        # Probe = the same frontier walk, writing nothing. Bounded so it stays cheap.
        print("--- PROBE (no writes) ---")
        if args.only != "companies":
            walk_firms(limit=args.limit or 400, dry_run=True)
        if args.only != "firms":
            asyncio.run(walk_companies(limit=args.limit or 1500, dry_run=True))
        return
    if args.command == "companies":
        new, _, stopped = asyncio.run(walk_companies(limit=args.limit, retry_only=args.retry_only))
        st = load_state()
        st["companies"] = {"new": len(new), "stopped": stopped, "at": time.strftime("%F %T")}
        save_state(st)
        return
    if args.command == "firms":
        new, _, stopped = walk_firms(limit=args.limit)
        st = load_state()
        st["firms"] = {"new": len(new), "stopped": stopped, "at": time.strftime("%F %T")}
        save_state(st)
        return
    if args.command == "txns":
        print("Transactions delta: widen YEARS + clear recent state, then run the existing")
        print("filler (it dedupes against what we hold):")
        print("   python3 mergr_fill_missing_txns.py")
        print("Not yet wired here — companies/firms first, per the gated plan.")


if __name__ == "__main__":
    main()
