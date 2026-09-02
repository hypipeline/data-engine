#!/usr/bin/env python3
"""
Mergr routine refresh — DEAL-LED.

STRATEGY (decided 28 Aug 2026, from measured evidence)
    Transactions lead. Companies are fetched ONLY when a new deal references one we do not
    already hold. Firms still get their cheap id sweep.

    The numbers behind that split, measured on this delta:
        companies  2,193 found by walking the id frontier -> only  345 (16%) are in any deal
        firms         37 found by sweeping the id range   ->       31 (84%) are in any deal
    The company frontier walk cost ~100 minutes for 84% noise. The firm sweep costs ~6
    minutes and is mostly signal — and firms are the buyer universe that feeds ON.

    A full company frontier walk (`mergr_delta.py companies`) stays available for an
    occasional annual rebuild; it is not part of the routine refresh.

LOOP-UNTIL-DRY
    New deals reveal parties, whose records reveal nothing further — but a fetch can fail
    transiently, so the loop repeats until the gap stops shrinking rather than assuming one
    pass is enough.

SKIP-FILE CAVEAT (the subtle one)
    An id that does not exist YET answers with a redirect, and the scrapers write redirects
    to the skip file as permanently dead. When Mergr later creates that record, the id is
    already blacklisted and no future delta will fetch it. So party ids are re-checked with
    the skip file IGNORED, and proven-live ids are removed from it. Measured 28 Aug 2026:
    66 skip-listed ids around June's high-water mark were live and referenced by new deals.

USAGE
    python3 mergr_sync.py --db mergr_scratch            # validate against a throwaway clone
    python3 mergr_sync.py --db mergr --years 2           # live local db, re-crawl last 2 years
    python3 mergr_sync.py --db mergr_scratch --skip-txns # start from the load (txns already done)

Respects the one-login-at-a-time rule: every stage runs sequentially, never concurrently.
"""
import argparse
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
FILL_STATE = os.path.join(BASE, "mergr_fill.state")
REPORT = os.path.join(BASE, "mergr_sync_report.json")


def sh(cmd, label):
    print(f"\n===== {label}\n$ {' '.join(cmd)}", flush=True)
    t = time.time()
    r = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(out[-3000:], flush=True)
    print(f"----- {label}: exit {r.returncode} in {time.time() - t:.0f}s", flush=True)
    return r.returncode, out


def db_counts(db):
    q = ("select (select count(*) from firms), (select count(*) from companies), "
         "(select count(*) from transactions), (select count(*) from transaction_parties);")
    r = subprocess.run(["docker", "exec", "mergr_db-db-1", "psql", "-U", "mergr", "-d", db,
                        "-tAF,", "-c", q], capture_output=True, text=True)
    try:
        f, c, t, p = r.stdout.strip().split(",")
        return {"firms": int(f), "companies": int(c), "transactions": int(t), "parties": int(p)}
    except ValueError:
        return {}


def clear_recent_years(n_years):
    """Re-crawl the last n years of transaction combos: the most recent year was only
    crawled up to the last run, and prior years gain retrospectively-added deals."""
    if not os.path.exists(FILL_STATE):
        return 0
    done = json.load(open(FILL_STATE))
    this_year = time.localtime().tm_year
    recent = {str(y) for y in range(this_year - n_years + 1, this_year + 1)}
    kept = [c for c in done if c.split("_")[0] not in recent]
    removed = len(done) - len(kept)
    if removed:
        json.dump(sorted(kept), open(FILL_STATE, "w"))
    print(f"transaction state: cleared {removed} combo(s) for years {sorted(recent)}")
    return removed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="mergr_scratch", help="target postgres database")
    # 3 years, i.e. back to 2024 as of Aug 2026. Craig's call, 29 Aug 2026, on measured yield:
    # re-crawling 2024-26 recovered 1,720 deals Mergr had added retrospectively — but the tail
    # falls off fast (2025: 1,271, 2026 H1: 281, 2024: only 168), so going deeper isn't worth the
    # login hours. A 2-year window would have missed the 2024 tail; 3 catches it.
    ap.add_argument("--years", type=int, default=3, help="how many recent years of deals to re-crawl")
    ap.add_argument("--skip-txns", action="store_true", help="transactions already crawled this session")
    ap.add_argument("--skip-firms", action="store_true", help="skip the cheap firm id sweep")
    ap.add_argument("--max-rounds", type=int, default=4)
    args = ap.parse_args()

    dsn = f"postgres://mergr:mergr@localhost:5433/{args.db}"
    env = dict(os.environ, DATABASE_URL=dsn, DATA_DIR=BASE, PYTHONUNBUFFERED="1")
    os.environ.update(DATABASE_URL=dsn, DATA_DIR=BASE)   # child processes via sh() inherit this
    started = time.strftime("%F %T")
    before = db_counts(args.db)
    print(f"=== mergr_sync {started} -> db '{args.db}'\nbefore: {before}", flush=True)
    log = {"started": started, "db": args.db, "before": before, "rounds": []}

    # ---- 1. transactions lead -------------------------------------------------------
    if not args.skip_txns:
        clear_recent_years(args.years)
        sh(["./run_until_clean.sh", "python3", "mergr_fill_missing_txns.py"], "transactions")

    # ---- 2. firms: cheap sweep, high signal ------------------------------------------
    if not args.skip_firms:
        sh(["./run_until_clean.sh", "python3", "mergr_delta.py", "firms"], "firm id sweep")

    # ---- 3. load -> gap -> fetch referenced parties -> repeat until dry ---------------
    for rnd in range(1, args.max_rounds + 1):
        subprocess.run([sys.executable, "import.py"], cwd=os.path.join(BASE, "mergr_db"),
                       env=env, capture_output=True, text=True)
        r = subprocess.run([sys.executable, "gap_analysis.py"], cwd=os.path.join(BASE, "mergr_db"),
                           env=dict(env, OUT_DIR=BASE), capture_output=True, text=True)
        print(f"\n===== round {rnd}: load + gap\n{r.stdout}", flush=True)

        miss_c = os.path.join(BASE, "mergr_missing_companies.txt")
        miss_f = os.path.join(BASE, "mergr_missing_firms.txt")
        n_c = sum(1 for l in open(miss_c)) if os.path.exists(miss_c) else 0
        n_f = sum(1 for l in open(miss_f)) if os.path.exists(miss_f) else 0
        log["rounds"].append({"round": rnd, "missing_companies": n_c, "missing_firms": n_f,
                              "counts": db_counts(args.db)})
        print(f"round {rnd}: gap = {n_c} companies, {n_f} firms", flush=True)
        if n_c == 0 and n_f == 0:
            print("gap is dry — nothing further referenced by any deal", flush=True)
            break

        # MANDATORY party pass. Transactions crawled from SEARCH LISTINGS frequently carry no
        # acquirer — the listing columns are often empty, and the acquirer lives on the deal's
        # detail page. Measured 29 Aug 2026: of 266 newly-crawled deals holding no acquirer, 178
        # had one on the detail page (De La Rue -> Atlas Holdings, and so on), and 33,507 deals
        # across the whole database were missing theirs. A deal with no acquirer contributes
        # nothing to Buyer Match deal-history mode, which rolls deals up to their acquirer — so
        # this runs on every sync, not when someone happens to spot it.
        needs = os.path.join(BASE, "mergr_txn_needs_parties.txt")
        r = subprocess.run(["docker", "exec", "mergr_db-db-1", "psql", "-U", "mergr", "-d", args.db, "-tAc",
                            "select t.transaction_id from transactions t where t.imported_at >= current_date - 1 "
                            "and not exists (select 1 from transaction_parties p "
                            "where p.transaction_id=t.transaction_id and p.role='acquirer')"],
                           capture_output=True, text=True)
        ids = [l.strip() for l in r.stdout.splitlines() if l.strip().isdigit()]
        log["rounds"][-1]["deals_missing_acquirer"] = len(ids)
        if ids:
            open(needs, "w").write("\n".join(ids) + "\n")
            sh(["./run_until_clean.sh", "python3", "mergr_scrape_txn_parties.py"],
               f"round {rnd}: detail-page parties for {len(ids)} acquirer-less deals")
            sh([sys.executable, "mergr_db/load_txn_parties.py", "--only-ids", needs],
               f"round {rnd}: load recovered parties")

        # Deals reference these ids, so the skip file must NOT gate the fetch.
        if n_c:
            sh(["./run_until_clean.sh", "python3", "mergr_delta.py", "recheck",
                "--ids-file", miss_c], f"round {rnd}: fetch {n_c} referenced companies")
        if n_f:
            sh(["./run_until_clean.sh", "python3", "mergr_delta.py", "recheck-firms",
                "--ids-file", miss_f], f"round {rnd}: fetch {n_f} referenced firms")

    # ---- 4. validate, and FAIL LOUDLY -------------------------------------------------
    # Every check here exists because the failure it catches actually happened and was found
    # by hand rather than by the pipeline. A sync that prints a happy summary over broken data
    # is worse than one that crashes.
    checks = {
        "error-page records saved as companies":
            "select count(*) from companies where name in "
            "('500 Internal Service Error','502 Bad Gateway','504 Gateway Time-out',"
            "'JavaScript is disabled','404 Not Found','Access Denied')",
        "new deals still lacking an acquirer":
            "select count(*) from transactions t where t.imported_at >= current_date - 1 and not exists "
            "(select 1 from transaction_parties p where p.transaction_id=t.transaction_id and p.role='acquirer')",
        "same entity as both acquirer and seller":
            "select count(*) from (select transaction_id, entity_type, entity_mergr_id from transaction_parties "
            "group by 1,2,3 having count(distinct role) > 1) d",
        "parties pointing at a transaction we do not hold":
            "select count(*) from transaction_parties p where not exists "
            "(select 1 from transactions t where t.transaction_id = p.transaction_id)",
        "companies with a blank name":
            "select count(*) from companies where coalesce(trim(name),'') = ''",
    }
    print("\n===== validation", flush=True)
    problems, results = [], {}
    for label, sql in checks.items():
        r = subprocess.run(["docker", "exec", "mergr_db-db-1", "psql", "-U", "mergr", "-d", args.db,
                            "-tAc", sql], capture_output=True, text=True)
        n = int(r.stdout.strip() or 0)
        results[label] = n
        flag = "OK  " if n == 0 else "FAIL"
        if n:
            problems.append(f"{label}: {n}")
        print(f"  [{flag}] {label}: {n}", flush=True)
    log["validation"] = results

    after = db_counts(args.db)
    log["after"] = after
    log["problems"] = problems
    log["delta"] = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    log["finished"] = time.strftime("%F %T")
    json.dump(log, open(REPORT, "w"), indent=1)
    print(f"\n=== DONE {log['finished']}\nafter : {after}\ndelta : {log['delta']}\n"
          f"report: {REPORT}", flush=True)
    if problems:
        print("\n*** SYNC FAILED VALIDATION — do not ship this to prod:", flush=True)
        for p in problems:
            print(f"***   {p}", flush=True)
        sys.exit(1)
    print("validation clean.", flush=True)


if __name__ == "__main__":
    main()
