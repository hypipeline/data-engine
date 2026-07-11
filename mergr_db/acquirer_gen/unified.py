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


def run_unified(conn, target, index=None, models=None, progress_cb=None):
    if index is None:
        index = verify.build_index(conn)
    models = models or DEFAULT_MODELS

    def run_one(provider, model, mode):
        cb = None
        if progress_cb:
            def cb(part, status, n):                 # model is stable (a run_one param, not a loop var)
                progress_cb(model, part, status, n)
        fn = generate.generate_split if mode == "split" else generate.generate
        # do_log=False here (shared conn isn't thread-safe); we log sequentially after.
        return fn(None, target, provider, model, dict(SETTINGS), index=index, do_log=False, on_progress=cb)

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

    rec_entries, deal_entries = [], []
    for r in per:
        model = _label(r.get("model", "?"))
        for a in r.get("acquirers", []):
            rec_entries.append(dict(a, models=[model]))
        for d in r.get("deals", []):
            deal_entries.append(dict(d, models=[model]))
    blist, counts = _build_buyers(index, rec_entries, deal_entries)
    try:
        verify.enrich_buyers(conn, blist)                # employees + Mergr/ON facts (free, small query)
    except Exception:                                    # noqa: BLE001  (enrichment is best-effort)
        conn.rollback()

    total_cost = round(sum(r.get("cost_usd", 0) for r in per), 4)
    return {
        "target": target,
        "buyers": blist,
        "models": [{"model": r.get("model"), "cost_usd": round(r.get("cost_usd", 0), 4),
                    "n_acquirers": len(r.get("acquirers", [])), "n_deals": len(r.get("deals", [])),
                    "latency_ms": r.get("latency_ms", 0), "error": r.get("error")} for r in per],
        "audit": [dict(c, model=_label(r.get("model", "?"))) for r in per for c in r.get("calls", [])],
        "total_cost": total_cost,
        "counts": counts,
    }


def _build_buyers(index, rec_entries, deal_entries):
    """Merge recommendation entries + deal entries into ranked buyer groups. Each entry carries a
    'models' list, so this works for both live per-model results and re-grouping cached merged data."""
    buyers = {}

    def get_buyer(name, website):
        k = verify.domain_of(website) or verify.norm_name(name)
        if not k:
            return None
        b = buyers.setdefault(k, {"name": name, "website": website, "type": None, "rationale": None,
                                  "source_url": None, "rec_sources": set(), "deals": [], "deal_sources": set()})
        if website and not b["website"]:
            b["website"] = website
        if name and not b["name"]:
            b["name"] = name
        return b

    for a in rec_entries:                                # recommended acquirers → buyer groups
        b = get_buyer(a.get("name"), a.get("website"))
        if not b:
            continue
        b["rec_sources"].update(a.get("models", []) or a.get("sources", []))
        if a.get("type") and not b["type"]:
            b["type"] = a.get("type")
        if a.get("source_url") and not b["source_url"]:
            b["source_url"] = a.get("source_url")
        if a.get("rationale") and (not b["rationale"] or len(a["rationale"]) < len(b["rationale"])):
            b["rationale"] = a["rationale"]

    for d in deal_entries:                               # precedent deals → nested under their acquirer
        if not verify.norm_name(d.get("acquirer")):
            continue
        b = get_buyer(d.get("acquirer"), d.get("acquirer_website"))
        if not b:
            continue
        mods = d.get("models", []) or d.get("sources", [])
        b["deal_sources"].update(mods)
        tk = verify.norm_name(d.get("target"))
        ex = next((x for x in b["deals"] if verify.norm_name(x.get("target")) == tk), None)
        if ex:
            ex.setdefault("sources", set()).update(mods)
        else:
            b["deals"].append(dict({k: v for k, v in d.items() if k not in ("models", "sources")},
                                   sources=set(mods)))

    blist = [{"name": b["name"], "website": b["website"], "type": b["type"], "rationale": b["rationale"],
              "source_url": b["source_url"], "rec_sources": sorted(b["rec_sources"]),
              "n_rec": len(b["rec_sources"]), "n_deals": len(b["deals"]),
              "deals": sorted([{**{k: v for k, v in dl.items() if k != "sources"},
                                "sources": sorted(dl.get("sources", [])), "n_sources": len(dl.get("sources", []))}
                               for dl in b["deals"]], key=lambda x: str(x.get("year") or ""), reverse=True)}
             for b in buyers.values()]
    blist = verify.match_acquirers(index, blist)         # ground each buyer

    for b in blist:
        v = b["verify"]
        st = v["status"]
        if not b["website"] and v.get("match_website"):
            b["website"] = v["match_website"]
        gb = 3 if st == "in_on" else (1 if st == "in_mergr" else 0)
        b["recommended"] = b["n_rec"] > 0
        b["proven"] = b["n_deals"] > 0
        b["both"] = b["recommended"] and b["proven"]
        b["score"] = b["n_rec"] * 3 + min(b["n_deals"], 8) + gb + (2 if b["both"] else 0)
        if v.get("on_buyer_id"):
            b["_selid"] = str(v["on_buyer_id"])
        elif st == "in_mergr" and v.get("match_id"):
            b["_selid"] = "m:" + (v.get("kind") or "firm") + ":" + str(v["match_id"])
        elif b["website"]:
            b["_selid"] = "w:" + verify.domain_of(b["website"])
        else:
            b["_selid"] = None
        b["can_tag"] = bool(b["website"])

    blist.sort(key=lambda b: (-b["score"], -b["n_rec"], b["name"].lower()))
    counts = {"buyers": len(blist),
              "both": sum(1 for b in blist if b["both"]),
              "consensus": sum(1 for b in blist if b["n_rec"] >= 2),
              "in_on": sum(1 for b in blist if b["verify"]["status"] == "in_on"),
              "in_mergr": sum(1 for b in blist if b["verify"]["status"] == "in_mergr"),
              "net_new": sum(1 for b in blist if b["verify"]["status"] == "none"),
              "deals": sum(b["n_deals"] for b in blist),
              "no_website": sum(1 for b in blist if not b["website"])}
    return blist, counts


def migrate_legacy(conn):
    """Rebuild buyer-view for cached searches stored in the old acquirers/deals format — FREE, no LLM.
    Reconstructs buyers from the stored merged acquirers (+ their model 'sources') and deals."""
    index = verify.build_index(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id, result FROM acquirer_gen.searches "
                    "WHERE result ? 'acquirers' AND NOT (result ? 'buyers')")
        rows = cur.fetchall()
    n = 0
    for sid, res in rows:
        rec = [dict(a, models=a.get("sources", [])) for a in (res.get("acquirers") or [])]
        deals = [dict(d, models=d.get("sources", [])) for d in (res.get("deals") or [])]
        blist, counts = _build_buyers(index, rec, deals)
        try:
            verify.enrich_buyers(conn, blist)
        except Exception:                                # noqa: BLE001
            conn.rollback()
        res.pop("acquirers", None)
        res["buyers"] = blist
        res["counts"] = counts
        with conn.cursor() as cur:
            cur.execute("UPDATE acquirer_gen.searches SET result=%s, n_acquirers=%s, n_consensus=%s WHERE id=%s",
                        (json.dumps(res), counts["buyers"], counts["consensus"], sid))
        conn.commit()
        n += 1
    return n
