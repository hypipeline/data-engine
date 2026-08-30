-- Provenance / rollback tagging for the Mergr public tables.
--
-- Every row carries the import batch that produced it. Batch 1 is "everything that existed
-- before we started tagging" — the 30 Jun 2026 baseline. Each later sync gets its own id, so a
-- bad import can be identified and reversed without restoring the whole database (which would
-- also destroy buyer_match / entity / linkedin, all of which live in this same database).
--
-- PERFORMANCE NOTE — this bit matters on prod:
-- `ADD COLUMN import_id integer DEFAULT 1 NOT NULL` is METADATA-ONLY in Postgres 11+. It does
-- not rewrite the table and returns instantly. The obvious-looking alternative — add the column
-- nullable, then `UPDATE ... SET import_id = 1` — rewrites every row, holds ACCESS EXCLUSIVE for
-- the duration and BLOCKS THE LIVE APP. That was tried on prod on 29 Aug 2026 and had to be
-- cancelled while the Data Engine home page hung behind it. Do not reintroduce the UPDATE.
--
-- Indexes are created CONCURRENTLY, outside any transaction, for the same reason.
-- Idempotent: safe to run twice.

CREATE TABLE IF NOT EXISTS mergr_imports (
    import_id   serial PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    note        text,
    source      text,
    row_counts  jsonb
);

INSERT INTO mergr_imports (import_id, started_at, finished_at, note, source)
SELECT 1, '2026-06-30 00:00:00+00', '2026-07-01 00:00:00+00',
       'Baseline: everything loaded before import tagging existed (30 Jun 2026 full scrape)',
       'mergr_scrape_* + import.py'
WHERE NOT EXISTS (SELECT 1 FROM mergr_imports WHERE import_id = 1);

ALTER TABLE firms               ADD COLUMN IF NOT EXISTS import_id integer NOT NULL DEFAULT 1;
ALTER TABLE companies           ADD COLUMN IF NOT EXISTS import_id integer NOT NULL DEFAULT 1;
ALTER TABLE transactions        ADD COLUMN IF NOT EXISTS import_id integer NOT NULL DEFAULT 1;
ALTER TABLE transaction_parties ADD COLUMN IF NOT EXISTS import_id integer NOT NULL DEFAULT 1;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_firms_import     ON firms (import_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_companies_import ON companies (import_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_txn_import       ON transactions (import_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_party_import     ON transaction_parties (import_id);

SELECT 'firms' AS t, import_id, count(*) FROM firms GROUP BY 1,2
UNION ALL SELECT 'companies', import_id, count(*) FROM companies GROUP BY 1,2
UNION ALL SELECT 'transactions', import_id, count(*) FROM transactions GROUP BY 1,2
UNION ALL SELECT 'transaction_parties', import_id, count(*) FROM transaction_parties GROUP BY 1,2
ORDER BY 1,2;
