#!/usr/bin/env python3
"""
Add + populate an indexed `domain` column on companies and firms, computed from
`website` via website_to_domain(). Enables fast exact domain search. Idempotent.
"""
import os, psycopg2
from psycopg2.extras import execute_values
from domain_utils import website_to_domain

DSN = os.environ["DATABASE_URL"]
conn = psycopg2.connect(DSN)
with conn, conn.cursor() as cur:
    for table, idcol in (("companies", "company_id"), ("firms", "firm_id")):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS domain TEXT")
        cur.execute(f"SELECT {idcol}, website FROM {table} WHERE website IS NOT NULL AND website<>''")
        rows = [(r[0], website_to_domain(r[1])) for r in cur.fetchall()]
        rows = [(i, d) for i, d in rows if d]
        cur.execute("CREATE TEMP TABLE _dom(id bigint, domain text) ON COMMIT DROP")
        execute_values(cur, "INSERT INTO _dom VALUES %s", rows, page_size=2000)
        cur.execute(f"UPDATE {table} t SET domain=_dom.domain FROM _dom WHERE t.{idcol}=_dom.id")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_domain ON {table}(domain)")
        print(f"{table}: set domain on {len(rows)} rows", flush=True)
    conn.commit()
print("done", flush=True)
