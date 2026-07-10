"""
Orchestrator: run a provider, ground the acquirers against ON/Mergr, compute cost, log the run.
Returns the enriched result (acquirers with .verify, deals, usage, cost_usd, counts).
"""
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

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


def generate_split(conn, target, provider, model, settings, index=None, do_log=True):
    """Two focused calls — ACQUIRERS-only and DEALS-only — run concurrently, then merged. Each fits a
    smaller token budget (no truncation, esp. DeepSeek's 8k ceiling) and gets the model's full attention.
    Logged as 'model (split)' so it compares against the combined run on the page."""
    if index is None:
        index = verify.build_index(conn)

    def one(part):
        return providers.run_provider(provider, model, target, dict(settings, part=part))

    with ThreadPoolExecutor(max_workers=2) as ex:
        fa, fd = ex.submit(one, "acquirers"), ex.submit(one, "deals")
        ra, rd = fa.result(), fd.result()

    usage = {k: ra["usage"].get(k, 0) + rd["usage"].get(k, 0)
             for k in ("input_tokens", "output_tokens", "web_searches")}
    res = {"provider": provider, "model": model, "text": "",
           "acquirers": ra.get("acquirers", []), "deals": rd.get("deals", []),
           "citations": (ra.get("citations") or []) + (rd.get("citations") or []),
           "usage": usage, "latency_ms": max(ra.get("latency_ms", 0), rd.get("latency_ms", 0)),
           "error": ra.get("error") or rd.get("error")}
    res["parse_ok"] = bool(res["acquirers"]) or bool(res["deals"])
    res["cost_usd"] = pricing.cost_usd(model, usage)
    res["acquirers"] = verify.match_acquirers(index, res["acquirers"])
    res["counts"] = verify.counts(res["acquirers"])
    res["counts"]["deals"] = len(res["deals"])
    res["model"] = model + " (split)"
    if do_log:
        try:
            log_run(conn, target, res, dict(settings, part="split"))
        except Exception:                            # noqa: BLE001
            conn.rollback()
    return res
