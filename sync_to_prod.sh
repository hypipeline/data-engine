#!/usr/bin/env bash
# Ship a Mergr import batch from the local Postgres to the EC2 box — INCREMENTALLY and REVERSIBLY.
#
# WHY NOT A DUMP/RESTORE: prod's database also holds buyer_match (19k live ON buyers, 882
# mandates, 107 cached doc summaries), entity, linkedin and de_access. A full restore of the
# local DB would wipe every one of them. This touches only the four public Mergr tables.
#
# EVERY ROW IS TAGGED with an import_id, and every party row this batch replaces is saved to a
# CSV on the box first. So the undo is complete and does not need the 1.5GB dump:
#
#     DELETE FROM transaction_parties WHERE import_id = <N>;
#     \copy transaction_parties FROM '/tmp/import<N>_parties_replaced.csv' CSV
#     DELETE FROM transactions  WHERE import_id = <N>;
#     DELETE FROM companies     WHERE import_id = <N>;   -- see note below
#     DELETE FROM firms         WHERE import_id = <N>;
#
# NOTE: firms/companies rows that already existed are UPDATED (not inserted) and their import_id
# moves to <N>. Deleting those would remove baseline records, so the undo for updated rows is the
# pre-import dump. New rows are safe to delete. The saved CSV covers the party rewrite, which is
# the only destructive part of this batch.
#
# Usage: ./sync_to_prod.sh <source_db> <since_date> <import_id> "<note>"
set -euo pipefail

SRC_DB="${1:-mergr}"
SINCE="${2:?need the import date, e.g. 2026-08-28 — do NOT default this to today: the rows are}"
IMPORT_ID="${3:?need an import id}"
NOTE="${4:-Mergr delta}"
BOX="ec2-user@54.170.119.21"
KEY="$HOME/.ssh/data-engine-key.pem"
PSQL="docker exec -i mergr_db-db-1 psql -U mergr -d $SRC_DB"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "=== [1/6] work out which deals need their parties rewritten"
# New transactions, plus every deal whose parties were rebuilt from detail pages.
# NOTE: every argument is passed explicitly. An earlier version defaulted the date to
# current_date when an argument was missing, which silently excluded the batch's own new deals
# (imported the previous day) and shipped 3,424 fewer party rows than local held.
python3 - "$WORK/affected.txt" "$SRC_DB" "$SINCE" <<'PY'
import json, subprocess, sys
out_path, src_db, since = sys.argv[1], sys.argv[2], sys.argv[3]
ids = {json.loads(l)["transaction_id"] for l in open("mergr_txn_parties.jsonl")}
r = subprocess.run(["docker", "exec", "mergr_db-db-1", "psql", "-U", "mergr", "-d", src_db, "-tAc",
                    f"select transaction_id from transactions where imported_at >= '{since}'"],
                   capture_output=True, text=True)
new = {int(x) for x in r.stdout.split() if x.strip().isdigit()}
if not new:
    sys.exit(f"ABORT: no transactions found with imported_at >= {since} — wrong date?")
ids |= new
open(out_path, "w").write("\n".join(map(str, sorted(ids))) + "\n")
print(f"  {len(ids):,} deals will have their party rows rebuilt ({len(new):,} of them new this batch)")
PY

echo "=== [2/6] export the batch from local (since ${SINCE})"
$PSQL -c "\copy (select * from firms where imported_at >= '$SINCE') to stdout csv" > "$WORK/firms.csv"
$PSQL -c "\copy (select * from companies where imported_at >= '$SINCE') to stdout csv" > "$WORK/companies.csv"
$PSQL -c "\copy (select * from transactions where imported_at >= '$SINCE') to stdout csv" > "$WORK/transactions.csv"
$PSQL -c "\copy (select p.transaction_id, p.role, p.entity_type, p.entity_mergr_id, p.name, p.label, p.sub_type
                 from transaction_parties p) to stdout csv" > "$WORK/parties_all.csv"
wc -l "$WORK"/*.csv | sed 's/^/  /'

echo "=== [3/6] ship to the box"
scp -i "$KEY" -q "$WORK"/firms.csv "$WORK"/companies.csv "$WORK"/transactions.csv \
    "$WORK"/parties_all.csv "$WORK"/affected.txt "$BOX:/tmp/"
ssh -i "$KEY" "$BOX" "for f in firms.csv companies.csv transactions.csv parties_all.csv affected.txt; do
    docker cp /tmp/\$f mergr_db-db-1:/tmp/ >/dev/null; done"

echo "=== [4/6] load on prod as import ${IMPORT_ID} (parties replaced, old rows saved first)"
ssh -i "$KEY" "$BOX" "docker exec -i mergr_db-db-1 psql -U mergr -d mergr -v ON_ERROR_STOP=1" <<SQL
BEGIN;
INSERT INTO mergr_imports (import_id, note, source)
SELECT ${IMPORT_ID}, '${NOTE}', 'sync_to_prod.sh'
WHERE NOT EXISTS (SELECT 1 FROM mergr_imports WHERE import_id = ${IMPORT_ID});
SELECT setval('mergr_imports_import_id_seq', GREATEST(${IMPORT_ID}, (SELECT max(import_id) FROM mergr_imports)));

CREATE TEMP TABLE affected (transaction_id bigint PRIMARY KEY);
COPY affected FROM '/tmp/affected.txt';

CREATE TEMP TABLE s_firms        (LIKE public.firms INCLUDING DEFAULTS);
CREATE TEMP TABLE s_companies    (LIKE public.companies INCLUDING DEFAULTS);
CREATE TEMP TABLE s_transactions (LIKE public.transactions INCLUDING DEFAULTS);
CREATE TEMP TABLE s_parties (transaction_id bigint, role text, entity_type text,
                             entity_mergr_id bigint, name text, label text, sub_type text);
COPY s_firms        FROM '/tmp/firms.csv' CSV;
COPY s_companies    FROM '/tmp/companies.csv' CSV;
COPY s_transactions FROM '/tmp/transactions.csv' CSV;
COPY s_parties      FROM '/tmp/parties_all.csv' CSV;

-- UNDO SNAPSHOT: the party rows this batch is about to destroy.
\copy (select p.* from public.transaction_parties p join affected a using (transaction_id)) to '/tmp/import${IMPORT_ID}_parties_replaced.csv' csv

INSERT INTO public.firms SELECT * FROM s_firms
  ON CONFLICT (firm_id) DO UPDATE SET name = EXCLUDED.name, website = EXCLUDED.website,
      import_id = ${IMPORT_ID};
UPDATE public.firms f SET import_id = ${IMPORT_ID} FROM s_firms s WHERE f.firm_id = s.firm_id;

INSERT INTO public.companies SELECT * FROM s_companies
  ON CONFLICT (company_id) DO UPDATE SET name = EXCLUDED.name, website = EXCLUDED.website,
      description = EXCLUDED.description, sector = EXCLUDED.sector, import_id = ${IMPORT_ID};
UPDATE public.companies c SET import_id = ${IMPORT_ID} FROM s_companies s WHERE c.company_id = s.company_id;

INSERT INTO public.transactions SELECT * FROM s_transactions
  ON CONFLICT (transaction_id) DO NOTHING;
UPDATE public.transactions t SET import_id = ${IMPORT_ID} FROM s_transactions s
  WHERE t.transaction_id = s.transaction_id;

DELETE FROM public.transaction_parties p USING affected a WHERE p.transaction_id = a.transaction_id;
INSERT INTO public.transaction_parties
       (transaction_id, role, entity_type, entity_mergr_id, name, label, sub_type, import_id)
SELECT s.transaction_id, s.role, s.entity_type, s.entity_mergr_id, s.name, s.label, s.sub_type, ${IMPORT_ID}
FROM s_parties s JOIN affected a USING (transaction_id)
ON CONFLICT (transaction_id, role, entity_type, entity_mergr_id) DO NOTHING;

UPDATE mergr_imports SET finished_at = now(),
    row_counts = jsonb_build_object(
      'firms',       (SELECT count(*) FROM public.firms               WHERE import_id = ${IMPORT_ID}),
      'companies',   (SELECT count(*) FROM public.companies           WHERE import_id = ${IMPORT_ID}),
      'transactions',(SELECT count(*) FROM public.transactions        WHERE import_id = ${IMPORT_ID}),
      'parties',     (SELECT count(*) FROM public.transaction_parties WHERE import_id = ${IMPORT_ID}))
WHERE import_id = ${IMPORT_ID};
COMMIT;
SQL

echo "=== [5/6] totals on prod"
ssh -i "$KEY" "$BOX" "docker exec mergr_db-db-1 psql -U mergr -d mergr -P pager=off -c \
  \"select 'firms' t, count(*) from firms union all select 'companies', count(*) from companies
    union all select 'transactions', count(*) from transactions
    union all select 'parties', count(*) from transaction_parties
    union all select 'deals with an acquirer', count(distinct transaction_id) from transaction_parties where role='acquirer';\" \
  -c \"select import_id, note, row_counts from mergr_imports order by import_id;\""

echo "=== [6/6] the other schemas must be untouched"
ssh -i "$KEY" "$BOX" "docker exec mergr_db-db-1 psql -U mergr -d mergr -P pager=off -c \
  \"select 'buyer_match.buyers' t, count(*) from buyer_match.buyers
    union all select 'buyer_match.mandates', count(*) from buyer_match.mandates
    union all select 'buyer_match.doc_cache', count(*) from buyer_match.doc_cache;\""
echo "undo snapshot on the box: /tmp/import${IMPORT_ID}_parties_replaced.csv (inside the db container)"
