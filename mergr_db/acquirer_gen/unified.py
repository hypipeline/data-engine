"""
Unified "comprehensive" run: fire the premium models concurrently, then merge their acquirers/deals
into ONE deduped list — flagging CONSENSUS (suggested by 2+ models) and grounding each against ON/Mergr.

Default trio: gpt-5.5 (split), claude-sonnet-4-6 (split), sonar-deep-research (combined). Each model
runs with do_log so cost history is kept; the merged view carries per-model + total cost.
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from acquirer_gen import generate, verify


def save_search(conn, result, input_mode="typed", mandate_code=None):
    """Persist a unified search to acquirer_gen.searches (history) and return its id."""
    c = result.get("counts", {})
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO acquirer_gen.searches
               (target, input_mode, mandate_code, total_cost, n_acquirers, n_deals, n_consensus, result)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (result.get("target"), input_mode, mandate_code, result.get("total_cost", 0),
             c.get("acquirers", 0), c.get("deals", 0), c.get("consensus_acquirers", 0),
             json.dumps(result)))
        sid = cur.fetchone()[0]
    conn.commit()
    return sid

# (provider, model, mode)  — mode: 'split' (2 focused calls) or 'combined' (one call)
DEFAULT_MODELS = [
    ("openai",     "gpt-5.5",             "split"),
    ("anthropic",  "claude-sonnet-4-6",   "split"),
    ("perplexity", "sonar-deep-research", "split"),   # split now works (structured output) + grounds better
]

SETTINGS = {"web": True, "n_acq": 100, "n_deals": 100, "max_searches": 20,
            "max_tokens": 16000, "search_context": "high", "effort": "medium"}


def _label(model):
    return model.replace(" (split)", "")


def _akey(a):
    """Dedupe key: registrable domain first (high precision), else normalized name."""
    return verify.domain_of(a.get("website")) or verify.norm_name(a.get("name"))


def _dkey(d):
    return (verify.norm_name(d.get("acquirer")), verify.norm_name(d.get("target")))


def run_unified(conn, target, index=None, models=None):
    if index is None:
        index = verify.build_index(conn)
    models = models or DEFAULT_MODELS

    def run_one(provider, model, mode):
        fn = generate.generate_split if mode == "split" else generate.generate
        # do_log=False here (shared conn isn't thread-safe); we log sequentially after.
        return fn(None, target, provider, model, dict(SETTINGS), index=index, do_log=False)

    per = []
    with ThreadPoolExecutor(max_workers=len(models)) as ex:
        futs = {ex.submit(run_one, p, m, mode): (p, m) for (p, m, mode) in models}
        for f in as_completed(futs):
            try:
                per.append(f.result())
            except Exception as e:                       # noqa: BLE001
                p, m = futs[f]
                per.append({"provider": p, "model": m, "acquirers": [], "deals": [],
                            "usage": {"input_tokens": 0, "output_tokens": 0, "web_searches": 0},
                            "cost_usd": 0, "error": str(e)[:300], "latency_ms": 0, "counts": {}})

    for r in per:                                        # persist each run for cost history
        try:
            generate.log_run(conn, target, r, {"unified": True})
        except Exception:                                # noqa: BLE001
            conn.rollback()

    # ── merge acquirers, tracking which models suggested each (consensus) ──
    macq = {}
    for r in per:
        model = _label(r.get("model", "?"))
        for a in r.get("acquirers", []):
            k = _akey(a)
            if not k:
                continue
            m = macq.setdefault(k, {"name": a.get("name"), "website": a.get("website"),
                                    "type": a.get("type"), "rationale": a.get("rationale"),
                                    "source_url": a.get("source_url"), "sources": set()})
            m["sources"].add(model)
            if a.get("website") and not m["website"]:
                m["website"] = a.get("website")
            if a.get("rationale") and (not m["rationale"] or len(a["rationale"]) < len(m["rationale"])):
                m["rationale"] = a["rationale"]           # prefer the terser rationale
    acq_list = [{"name": v["name"], "website": v["website"], "type": v["type"],
                 "rationale": v["rationale"], "source_url": v["source_url"],
                 "sources": sorted(v["sources"]), "n_sources": len(v["sources"])}
                for v in macq.values()]
    acq_list = verify.match_acquirers(index, acq_list)    # ground the merged list once
    # consensus first, then grounded, then buyer-recall
    rank = {"in_on": 2, "in_mergr": 1, "none": 0}
    acq_list.sort(key=lambda a: (-a["n_sources"], -rank.get(a["verify"]["status"], 0), a["name"].lower()))

    # ── merge deals ──
    mdeal = {}
    for r in per:
        model = _label(r.get("model", "?"))
        for d in r.get("deals", []):
            k = _dkey(d)
            if not any(k):
                continue
            m = mdeal.setdefault(k, dict(d, sources=set()))
            m["sources"].add(model)
            for fld in ("acquirer_website", "target_website", "year", "value", "source_url"):
                if d.get(fld) and not m.get(fld):
                    m[fld] = d[fld]
    deal_list = [{**{kk: vv for kk, vv in v.items() if kk != "sources"},
                  "sources": sorted(v["sources"]), "n_sources": len(v["sources"])}
                 for v in mdeal.values()]
    deal_list.sort(key=lambda d: (-d["n_sources"], str(d.get("year") or ""), ))

    total_cost = round(sum(r.get("cost_usd", 0) for r in per), 4)
    return {
        "target": target,
        "acquirers": acq_list,
        "deals": deal_list,
        "models": [{"model": r.get("model"), "cost_usd": round(r.get("cost_usd", 0), 4),
                    "n_acquirers": len(r.get("acquirers", [])), "n_deals": len(r.get("deals", [])),
                    "latency_ms": r.get("latency_ms", 0), "error": r.get("error")} for r in per],
        # full audit — every model call's exact prompt IN and raw output OUT, for validation
        "audit": [dict(c, model=_label(r.get("model", "?"))) for r in per for c in r.get("calls", [])],
        "total_cost": total_cost,
        "counts": {"acquirers": len(acq_list), "deals": len(deal_list),
                   "consensus_acquirers": sum(1 for a in acq_list if a["n_sources"] >= 2),
                   "in_on": sum(1 for a in acq_list if a["verify"]["status"] == "in_on"),
                   "in_mergr": sum(1 for a in acq_list if a["verify"]["status"] == "in_mergr"),
                   "net_new": sum(1 for a in acq_list if a["verify"]["status"] == "none")},
    }
