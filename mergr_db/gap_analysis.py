#!/usr/bin/env python3
"""
Find Mergr entities referenced by transactions that we don't yet hold a record
for, and write them to persistent queue files the scrapers can consume:

    mergr_missing_companies.txt   (one company mergr_id per line)
    mergr_missing_firms.txt       (one firm mergr_id per line)

"Referenced" = appears as a transaction target (always a company) or as an
acquirer/seller party (firm or company). "Held" = present in companies/firms.

Idempotent: rewrites the files each run with the current outstanding set.

Env:
  DATABASE_URL   postgres connection string
  OUT_DIR        where to write the .txt files (default: ..  = project root)
"""
import os
import psycopg2

DSN     = os.environ["DATABASE_URL"]
OUT_DIR = os.environ.get("OUT_DIR", "..")

MISSING_COMPANIES = """
SELECT DISTINCT mid FROM (
    -- companies referenced as transaction targets
    SELECT target_mergr_id AS mid FROM transactions WHERE target_mergr_id IS NOT NULL
    UNION
    -- companies referenced as acquirer/seller parties
    SELECT entity_mergr_id AS mid FROM transaction_parties WHERE entity_type = 'company'
) ref
LEFT JOIN companies c ON c.company_id = ref.mid
WHERE c.company_id IS NULL
ORDER BY mid;
"""

MISSING_FIRMS = """
SELECT DISTINCT p.entity_mergr_id AS mid
FROM transaction_parties p
LEFT JOIN firms f ON f.firm_id = p.entity_mergr_id
WHERE p.entity_type = 'firms' AND f.firm_id IS NULL
ORDER BY mid;
"""


def write_ids(cur, sql, path, label):
    cur.execute(sql)
    ids = [r[0] for r in cur.fetchall()]
    with open(path, "w") as fh:
        fh.write("\n".join(str(i) for i in ids))
        if ids:
            fh.write("\n")
    print(f"  {label}: {len(ids):>7} missing -> {path}", flush=True)
    return len(ids)


def main():
    conn = psycopg2.connect(DSN)
    with conn, conn.cursor() as cur:
        print("Gap analysis:", flush=True)
        write_ids(cur, MISSING_COMPANIES,
                  os.path.join(OUT_DIR, "mergr_missing_companies.txt"), "companies")
        write_ids(cur, MISSING_FIRMS,
                  os.path.join(OUT_DIR, "mergr_missing_firms.txt"), "firms")
    conn.close()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
