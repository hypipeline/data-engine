"""
Orchestrator: run a provider, ground the acquirers against ON/Mergr, compute cost, log the run.
Returns the enriched result (acquirers with .verify, deals, usage, cost_usd, counts).
"""
import hashlib
import json
import os

import psycopg2
import psycopg2.extras

from acquirer_gen import pricing, providers, verify

PG_DSN = os.environ.get("DATABASE_URL", "postgres://mergr:mergr@127.0.0.1:5433/mergr")


def _hash(target, provider, model, settings):
    return hashlib.sha256(
        json.dumps([target, provider, model, settings], sort_keys=True).encode()).hexdigest()


def log_run(conn, target, res, settings):
    c = res.get("counts", {})
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO acquirer_gen.runs
               (target,provider,model,settings,prompt_hash,input_tokens,output_tokens,web_searches,
                cost_usd,latency_ms,n_acquirers,n_deals,n_in_on,n_in_mergr,n_net_new,parse_ok,error,result)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (target, res["provider"], res["model"], json.dumps(settings),
             _hash(target, res["provider"], res["model"], settings),
             res["usage"]["input_tokens"], res["usage"]["output_tokens"], res["usage"]["web_searches"],
             res.get("cost_usd", 0), res.get("latency_ms", 0),
             c.get("total", 0), len(res.get("deals", [])),
             c.get("in_on", 0), c.get("in_mergr", 0), c.get("net_new", 0),
             res.get("parse_ok", False), res.get("error"),
             json.dumps({"acquirers": res.get("acquirers", []), "deals": res.get("deals", []),
                         "citations": res.get("citations", [])})))
    conn.commit()


def generate(conn, target, provider, model, settings, index=None, do_log=True):
    res = providers.run_provider(provider, model, target, settings)
    res["cost_usd"] = pricing.cost_usd(model, res["usage"])
    if index is None:
        index = verify.build_index(conn)
    res["acquirers"] = verify.match_acquirers(index, res.get("acquirers", []))
    res["counts"] = verify.counts(res["acquirers"])
    res["counts"]["deals"] = len(res.get("deals", []))
    if do_log:
        try:
            log_run(conn, target, res, settings)
        except Exception:                            # noqa: BLE001  (logging must not break a run)
            conn.rollback()
    return res
