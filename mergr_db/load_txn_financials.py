#!/usr/bin/env python3
"""
Load scraped transaction financials (mergr_txn_details/*.json) into the
transactions table's financial columns. Idempotent; safe to run on partial
results while the scrape is still going.
"""
import os, glob, json, psycopg2
from psycopg2.extras import execute_values

DSN = os.environ["DATABASE_URL"]
DETAILS = os.path.join(os.environ.get("DATA_DIR", ".."), "mergr_txn_details")
FIN_KEYS = ("deal_value", "revenue", "ebitda", "ev_revenue", "ev_ebitda")

rows = []
n_files = 0
for f in glob.glob(DETAILS + "/*.json"):
    n_files += 1
    try:
        d = json.load(open(f))
    except Exception:
        continue
    if any(d.get(k) is not None for k in FIN_KEYS):
        rows.append((
            d["transaction_id"], d.get("deal_value"), d.get("deal_value_currency"),
            d.get("revenue"), d.get("revenue_currency"), d.get("ebitda"),
            d.get("ebitda_currency"), d.get("ev_revenue"), d.get("ev_ebitda")))

print(f"scanned {n_files} detail files, {len(rows)} with financials", flush=True)
conn = psycopg2.connect(DSN)
with conn, conn.cursor() as cur:
    cur.execute("""CREATE TEMP TABLE _fin(
        transaction_id bigint, deal_value numeric, deal_value_currency text,
        revenue numeric, revenue_currency text, ebitda numeric, ebitda_currency text,
        ev_revenue numeric, ev_ebitda numeric)""")
    execute_values(cur, "INSERT INTO _fin VALUES %s", rows, page_size=1000)
    cur.execute("""UPDATE transactions t SET
        deal_value=f.deal_value, deal_value_currency=f.deal_value_currency,
        revenue=f.revenue, revenue_currency=f.revenue_currency,
        ebitda=f.ebitda, ebitda_currency=f.ebitda_currency,
        ev_revenue=f.ev_revenue, ev_ebitda=f.ev_ebitda,
        financials_scraped_at=now()
        FROM _fin f WHERE t.transaction_id=f.transaction_id""")
    print(f"updated {cur.rowcount} transactions with financials", flush=True)
