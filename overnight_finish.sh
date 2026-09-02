#!/usr/bin/env bash
# Overnight chain: finish the Mergr delta end to end, unattended.
#   1. wait for the deal-led gap loop on the scratch db to finish
#   2. load the same JSON into the LIVE LOCAL db
#   3. ship the delta to prod (scoped, incremental, with a rollback dump taken first)
#   4. re-run the ON buyer anti-join so the new PE firms are triaged
#   5. write a morning report
#
# Everything sequential — one Mergr login rule still applies (though only step 4 touches the
# network beyond the box).
set -uo pipefail
cd "$(dirname "$0")"
SP="/private/tmp/claude-501/-Users-craiganderson-Dropbox-dev-on-testing/e2be28c8-7b7f-457a-82a6-80bf50a8dd8f/scratchpad"
REPORT="$SP/MORNING_REPORT.md"
say() { echo "[$(date '+%F %T')] $*" | tee -a "$SP/overnight.log"; }

say "waiting for mergr_sync (scratch gap loop) to finish"
while pgrep -f "mergr_sync.py" > /dev/null; do sleep 20; done
say "mergr_sync finished"

say "loading the delta into the LIVE LOCAL db (mergr)"
DATABASE_URL="postgres://mergr:mergr@localhost:5433/mergr" DATA_DIR="$PWD" \
    python3 mergr_db/import.py >> "$SP/overnight.log" 2>&1
say "local load exit $?"

say "shipping the delta to prod"
./sync_to_prod.sh mergr "$(date +%F)" >> "$SP/prod_sync.log" 2>&1
say "prod sync exit $?"

say "restarting local web/api (stopped earlier for the scratch clone)"
docker compose -f mergr_db/docker-compose.yml start web api >> "$SP/overnight.log" 2>&1

say "triaging the new PE firms against LIVE ON"
python3 check_new_firms_vs_on.py "$SP/new_firms_vs_on.txt" >> "$SP/overnight.log" 2>&1
say "anti-join exit $?"

say "writing morning report"
{
  echo "# Mergr delta — completed overnight $(date '+%F %T')"
  echo
  echo '## Local (live) database'
  docker exec mergr_db-db-1 psql -U mergr -d mergr -P pager=off -c \
    "select 'firms' t, count(*) from firms union all select 'companies', count(*) from companies
     union all select 'transactions', count(*) from transactions
     union all select 'parties', count(*) from transaction_parties;"
  echo
  echo '## Prod database (EC2)'
  ssh -i ~/.ssh/data-engine-key.pem ec2-user@54.170.119.21 \
    "docker exec mergr_db-db-1 psql -U mergr -d mergr -P pager=off -c \
     \"select 'firms' t, count(*) from firms union all select 'companies', count(*) from companies
       union all select 'transactions', count(*) from transactions
       union all select 'parties', count(*) from transaction_parties;\""
  echo
  echo '## Prod other schemas (must be unchanged)'
  ssh -i ~/.ssh/data-engine-key.pem ec2-user@54.170.119.21 \
    "docker exec mergr_db-db-1 psql -U mergr -d mergr -P pager=off -c \
     \"select 'buyer_match.buyers' t, count(*) from buyer_match.buyers
       union all select 'buyer_match.mandates', count(*) from buyer_match.mandates
       union all select 'buyer_match.doc_cache', count(*) from buyer_match.doc_cache;\""
  echo
  echo '## New PE firms not already in ON'
  cat "$SP/new_firms_vs_on.txt" 2>/dev/null || echo '(anti-join did not run)'
} > "$REPORT" 2>&1
say "DONE — report at $REPORT"
