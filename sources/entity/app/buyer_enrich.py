"""
Buyer LinkedIn enrichment — one-off / cron-able background pass over Buyer Match buyers.

For each buyer that has no employee count, resolve its website to a LinkedIn company page
(Google via Bright Data Web Unlocker) and pull the LinkedIn Organization data — headline being
the employee count. Results are written to buyer_match.buyer_linkedin, the shared
linkedin.companies cache is populated, and buyer_match.buyers.no_of_employees is gap-filled
(only where NULL — never clobbers a value already there).

Idempotent + resumable: a buyer already in buyer_match.buyer_linkedin is skipped, so the job
can be stopped and restarted freely. Slow/gentle by design (small worker pool, no hammering).

Run inside the entity container (has DATABASE_URL + Bright Data config + LookupTools):
    python buyer_enrich.py
Env:
    BM_ENRICH_WORKERS  concurrency (default 4)
    BM_ENRICH_LIMIT    cap rows this run (default: all pending) — handy for a test pass
    BM_ENRICH_ALL      '1' to process ALL buyers, not just those missing an employee count
"""
from __future__ import annotations

import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from urllib.parse import urlparse

import psycopg2

from config import load_config
from tools import LookupTools
import linkedin_cache

CONFIG = load_config()
DSN = os.environ.get("DATABASE_URL")
WORKERS = int(os.environ.get("BM_ENRICH_WORKERS", "4"))
LIMIT = int(os.environ.get("BM_ENRICH_LIMIT", "0"))          # 0 = all pending
ALL = os.environ.get("BM_ENRICH_ALL", "") == "1"
COST_PER_REQ = 0.0015                                         # ~Bright Data Web Unlocker CPM


def _conn():
    return psycopg2.connect(DSN)


def ensure_table():
    with closing(_conn()) as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS buyer_match.buyer_linkedin (
                buyer_id     bigint PRIMARY KEY,
                query        text,
                linkedin_url text,
                employees    int,
                name         text,
                website      text,
                address      text,
                status       text,          -- ok | no_linkedin | no_data | no_input | error
                checked_at   timestamptz DEFAULT now()
            );
        """)
        c.commit()


def norm_domain(website: str) -> str:
    w = (website or "").strip()
    if not w:
        return ""
    if "://" not in w:
        w = "http://" + w
    host = urlparse(w).hostname or ""
    return re.sub(r"^www\.", "", host).lower()


def pending_buyers():
    where = "" if ALL else "AND b.no_of_employees IS NULL"
    sql = f"""
        SELECT b.id, b.name, b.website
        FROM buyer_match.buyers b
        WHERE b.embedding IS NOT NULL
          AND b.website IS NOT NULL AND btrim(b.website) <> ''
          {where}
          AND NOT EXISTS (SELECT 1 FROM buyer_match.buyer_linkedin bl WHERE bl.buyer_id = b.id)
        ORDER BY b.id
    """
    if LIMIT:
        sql += f" LIMIT {LIMIT}"
    with closing(_conn()) as c, c.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


# ── per-buyer work ────────────────────────────────────────────────────────────
def process(row):
    """Returns (status, brightdata_calls). Writes buyer_linkedin + gap-fills buyers."""
    bid, name, website = row
    query = norm_domain(website) or (name or "").strip().lower()
    if not query:
        _write(bid, "", None, None, None, None, "no_input")
        return "no_input", 0

    calls = 0
    data = linkedin_cache.get_latest(query)               # reuse anything already looked up
    if not data:
        t = LookupTools(CONFIG)
        try:
            url = t.find_linkedin_url(query)
        except Exception:                                  # noqa: BLE001
            url = None
        calls += 1
        data = None
        if url:
            try:
                d = t.linkedin_company_data(url)
            except Exception:                              # noqa: BLE001
                d = None
            calls += 1
            data = {
                "query": query,
                "linkedin_url": url,
                "name": (d or {}).get("name"),
                "employees": (d or {}).get("employees"),
                "website": (d or {}).get("website"),
                "address": (d or {}).get("address"),
                "description": (d or {}).get("description"),
                "slogan": (d or {}).get("slogan"),
                "org": (d or {}).get("org"),
                "from_cache": False,
            }
            if d:                                          # cache genuine successes only
                try:
                    linkedin_cache.save(query, data)
                except Exception:                          # noqa: BLE001
                    pass

    if not data or not data.get("linkedin_url"):
        _write(bid, query, None, None, None, None, "no_linkedin")
        return "no_linkedin", calls

    emp = data.get("employees")
    status = "ok" if emp is not None else "no_data"
    _write(bid, query, data.get("linkedin_url"), emp, data.get("name"),
           data.get("website"), status, address=data.get("address"))
    return status, calls


def _write(bid, query, url, emp, li_name, li_site, status, address=None):
    with closing(_conn()) as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO buyer_match.buyer_linkedin "
            "(buyer_id, query, linkedin_url, employees, name, website, address, status, checked_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now()) "
            "ON CONFLICT (buyer_id) DO UPDATE SET "
            "  query=EXCLUDED.query, linkedin_url=EXCLUDED.linkedin_url, employees=EXCLUDED.employees, "
            "  name=EXCLUDED.name, website=EXCLUDED.website, address=EXCLUDED.address, "
            "  status=EXCLUDED.status, checked_at=now()",
            (bid, query, url, emp, li_name, li_site, address, status))
        if emp is not None:                                # gap-fill only; never overwrite
            cur.execute("UPDATE buyer_match.buyers SET no_of_employees=%s "
                        "WHERE id=%s AND no_of_employees IS NULL", (emp, bid))
        c.commit()


# ── driver ────────────────────────────────────────────────────────────────────
def main():
    if not DSN:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    ensure_table()
    rows = pending_buyers()
    n = len(rows)
    print(f"[enrich] {n} buyers pending (workers={WORKERS}, all={ALL}, limit={LIMIT or 'none'})", flush=True)
    if not n:
        print("[enrich] nothing to do.", flush=True)
        return

    start = time.time()
    done = calls = 0
    tally = {"ok": 0, "no_data": 0, "no_linkedin": 0, "no_input": 0, "error": 0}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(process, r): r for r in rows}
        for fut in as_completed(futs):
            done += 1
            try:
                status, c = fut.result()
            except Exception as e:                         # noqa: BLE001
                status, c = "error", 0
                bid = futs[fut][0]
                try:
                    _write(bid, "", None, None, None, None, "error")
                except Exception:                          # noqa: BLE001
                    pass
                print(f"[enrich] buyer {bid} error: {e}", flush=True)
            tally[status] = tally.get(status, 0) + 1
            calls += c
            if done % 25 == 0 or done == n:
                el = time.time() - start
                rate = done / el if el else 0
                eta = (n - done) / rate if rate else 0
                print(f"[enrich] {done}/{n} | ok={tally['ok']} no_data={tally['no_data']} "
                      f"no_li={tally['no_linkedin']} err={tally['error']} | "
                      f"~${calls * COST_PER_REQ:,.2f} | {rate:.2f}/s | ETA {eta/60:,.0f}m",
                      flush=True)

    el = time.time() - start
    print(f"[enrich] DONE {done}/{n} in {el/60:,.1f}m | "
          f"ok(with employees)={tally['ok']} no_data={tally['no_data']} "
          f"no_linkedin={tally['no_linkedin']} error={tally['error']} | "
          f"~{calls} Bright Data calls ≈ ${calls * COST_PER_REQ:,.2f}", flush=True)


if __name__ == "__main__":
    main()
