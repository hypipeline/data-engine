#!/usr/bin/env bash
# Self-healing runner for the resumable Mergr scrapers.
#
# The original 8-hour detail scrape survived ~7 browser crashes only because of an ad-hoc
# shell loop typed at the time, which was never committed — so it had to be re-created.
# This is it, committed. Every Mergr scraper is resumable (skip-existing on disk), so a
# relaunch always continues rather than repeating work.
#
# Wraps each attempt in caffeinate so the Mac never sleeps mid-run.
#
# Usage:
#   ./run_until_clean.sh python3 mergr_delta.py companies
#   MAX_TRIES=10 ./run_until_clean.sh python3 mergr_delta.py firms
#
# Remember: ONE Mergr login at a time. Do not run two of these at once.
set -uo pipefail

# Unbuffered, or Python holds progress lines in a 4-8KB buffer when stdout is a file and a
# multi-hour run looks silent — you cannot tell a healthy walk from a stalled one.
export PYTHONUNBUFFERED=1

MAX_TRIES=${MAX_TRIES:-40}
RETRY_WAIT=${RETRY_WAIT:-15}
tries=0

echo "[runner] $(date '+%F %T') starting: $*"
until caffeinate -dims "$@"; do
    rc=$?
    tries=$((tries + 1))
    if [ "$tries" -ge "$MAX_TRIES" ]; then
        echo "[runner] $(date '+%F %T') giving up after $tries relaunch(es), last exit $rc"
        exit "$rc"
    fi
    echo "[runner] $(date '+%F %T') exit $rc — relaunch #$tries in ${RETRY_WAIT}s"
    sleep "$RETRY_WAIT"
done
echo "[runner] $(date '+%F %T') clean exit after $tries relaunch(es)"
