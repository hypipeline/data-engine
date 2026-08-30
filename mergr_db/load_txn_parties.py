#!/usr/bin/env python3
"""
Load mergr_txn_parties.jsonl (detail-page acquirers/sellers) into transaction_parties.

WHY THIS EXISTS: transaction rows scraped from SEARCH LISTINGS often omit the parties — the
acquirer and seller are reliably present only on each deal's DETAIL page. Verified against live
Mergr on 29 Aug 2026: of 266 newly-crawled deals holding no acquirer, 264 had one on the detail
page (Lennox International, Atlas Holdings, Silver Oak Services Partners...). A deal with no
acquirer is invisible to Buyer Match deal-history mode, which rolls deals up to their acquirer,
so this pass is not optional after a listing-led crawl.

`mergr_scrape_txn_parties.py` produces the jsonl; this loads it. Idempotent — the natural key
(transaction_id, role, entity_type, entity_mergr_id) carries a unique constraint and conflicts
are skipped, so re-running never duplicates.

  DATABASE_URL=postgres://... python3 load_txn_parties.py [path/to/mergr_txn_parties.jsonl] [--only-ids FILE]
"""
import json
import os
import sys

import psycopg2
import psycopg2.extras

DSN = os.environ["DATABASE_URL"]
DEFAULT_JSONL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "mergr_txn_parties.jsonl")

SQL = """
INSERT INTO transaction_parties (transaction_id, role, entity_type, entity_mergr_id, name)
VALUES %s
ON CONFLICT (transaction_id, role, entity_type, entity_mergr_id) DO NOTHING
"""


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("jsonl", nargs="?", default=DEFAULT_JSONL)
    ap.add_argument("--only-ids", help="restrict to transaction ids listed in this file")
    ap.add_argument("--replace", action="store_true",
                    help="rebuild these deals' parties from detail data (detail page wins over listing)")
    a = ap.parse_args()
    path = a.jsonl
    only = ({int(l) for l in open(a.only_ids) if l.strip().isdigit()} if a.only_ids else None)

    rows, deals, skipped = [], 0, 0
    for line in open(path):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        tid = d.get("transaction_id")
        if only is not None and tid not in only:
            continue
        deals += 1
        for role, key in (("acquirer", "acquirers"), ("seller", "sellers")):
            for p in d.get(key) or []:
                et, mid = p.get("entity_type"), p.get("mergr_id")
                if not et or mid is None:
                    skipped += 1
                    continue
                # the scraper emits the URL segment; the schema uses 'firms' / 'company'
                et = "firms" if et.startswith("firm") else "company"
                rows.append((tid, role, et, mid, p.get("name")))

    conn = psycopg2.connect(DSN)
    with conn, conn.cursor() as cur:
        cur.execute("select count(*) from transaction_parties")
        before = cur.fetchone()[0]
        if "--replace" in sys.argv:
            # The DETAIL page is authoritative. The listing parser takes parties from fixed
            # columns and demonstrably mis-assigns them — verified 29 Aug 2026 on txn 854,
            # where Mergr's page shows Grupo MRS as the investor and Quilvest/MCH as sellers,
            # while our listing-derived rows had Grupo MRS as a SELLER and no acquirer at all.
            # So for any deal we have detail data for, its rows are rebuilt from that data
            # rather than merged with the listing's version.
            tids = sorted({r[0] for r in rows})
            cur.execute("delete from transaction_parties where transaction_id = any(%s)", (tids,))
            print(f"--replace: cleared party rows for {len(tids)} deal(s) ({cur.rowcount} rows)")
        psycopg2.extras.execute_values(cur, SQL, rows, page_size=1000)
        cur.execute("select count(*) from transaction_parties")
        after = cur.fetchone()[0]
    conn.close()
    print(f"{deals} deal(s) read, {len(rows)} party row(s) offered, {skipped} unusable")
    print(f"transaction_parties: {before:,} -> {after:,}  (+{after - before:,} inserted, "
          f"{len(rows) - (after - before)} already present)")


if __name__ == "__main__":
    main()
