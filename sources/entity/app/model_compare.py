"""
Model-comparison tester — the stage AFTER the coverage harness.

Coverage tests the SEARCH (fetch + extract + registries; it greps the evidence). THIS tests the
ANALYSIS: it reproduces the exact (system_prompt, user_message) that production's Phase 6 builds
for a case, CACHES that Phase-1 input, then fans it out across candidate models and compares the
recommended entity, cost and latency.

Routing (per user decision): use a provider's DIRECT API when we hold that provider's key,
otherwise go through OpenRouter. The baseline column is anthropic/claude-sonnet-4-6 run DIRECT,
i.e. exactly as production runs it. OpenRouter calls pin the provider (no silent fallbacks) and
record which upstream actually served the request, so the benchmark is reproducible.

Reuses coverage_cases as the example set and EntityLookup for a faithful pipeline + JSON parsing.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from psycopg2.extras import RealDictCursor

import coverage                      # reuse DB connection + the shared example cases
from agent import EntityLookup       # faithful pipeline phases + parse_json_response (w/ salvage)

# ── candidate models (OpenRouter-style ids; editable from the UI) ─────────────
DEFAULT_MODELS = [
    "anthropic/claude-sonnet-4-6",          # baseline — routed DIRECT (= production)
    "anthropic/claude-opus-4.8",            # ceiling: does a bigger Anthropic model judge better?
    "openai/gpt-5",                         # frontier, mid price (1.25/10)
    "openai/gpt-5.6-sol",                   # top OpenAI flagship tier (5/30)
    "google/gemini-2.5-pro",                # frontier (1.25/10)
    "anthropic/claude-haiku-4.5",           # cheaper candidate (~1/3 of Sonnet)
    "google/gemini-2.5-flash",              # ~10x cheaper
    "deepseek/deepseek-chat",               # ~15x cheaper
    "moonshotai/kimi-k2-thinking",          # Kimi K2 (reasoning) — cheap reasoner (~$30/1k)
    "moonshotai/kimi-k3",                    # Kimi K3 — newer/bigger (1M ctx) but Sonnet-tier price
]

# per-1M-token (input, output) USD for DIRECT providers, keyed by the provider's NATIVE model id.
# OpenRouter returns its own cost, so we only need rates for models we call directly. Unknown →
# cost reported as null (not guessed).
_DIRECT_RATES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-5": (1.25, 10.0),
    "gpt-5.6-sol": (5.0, 30.0),
}


def _direct_cost(model_name, it, ot):
    r = _DIRECT_RATES.get(model_name)
    return (it * r[0] / 1e6 + ot * r[1] / 1e6) if r else None

MAX_TOKENS = 16384                   # match production analysis cap


# ── OpenRouter key (gitignored secrets file, or env) ──────────────────────────
def openrouter_key():
    k = os.environ.get("OPENROUTER_API_KEY")
    if k:
        return k
    candidates = ["/app/openrouter.secrets.env"]                          # container path
    try:
        candidates.append(str(pathlib.Path(__file__).resolve().parents[3] / "openrouter.secrets.env"))  # repo root (local)
    except IndexError:
        pass
    for p in candidates:
        try:
            for line in open(p):
                if line.strip().startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
    return None


# ── provider routing ──────────────────────────────────────────────────────────
def resolve_route(model_ref: str, config: dict) -> str:
    """Direct when we hold that provider's key, else openrouter."""
    prov = model_ref.split("/", 1)[0].lower()
    if prov == "anthropic" and config.get("anthropic_api_key"):
        return "anthropic"
    if prov == "openai" and config.get("openai_api_key"):
        return "openai"
    return "openrouter"


# ── low-level transports ──────────────────────────────────────────────────────
_TRANSIENT = {429, 500, 502, 503, 529}   # rate-limit / provider-overload / gateway → worth retrying


def _post(url, headers, body, timeout=240, retries=2):
    """POST with retry+backoff on transient statuses (429 rate-limit, 5xx) — premium/low-capacity
    models like kimi-k3 get rate-limited upstream, especially under our parallel + multi-case load."""
    t0 = time.time()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
            return r.status, json.load(r), int((time.time() - t0) * 1000), None
        except urllib.error.HTTPError as e:
            code = e.code
            text = e.read().decode(errors='replace')[:400]
            if code in _TRANSIENT and attempt < retries:
                time.sleep(2 * (attempt + 1))          # 2s, then 4s
                continue
            return code, None, int((time.time() - t0) * 1000), f"HTTP {code}: {text}"
        except Exception as e:  # noqa: BLE001
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return 0, None, int((time.time() - t0) * 1000), f"{type(e).__name__}: {e}"


def _call_anthropic(model_name, system, user, key):
    status, d, ms, err = _post(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        {"model": model_name, "max_tokens": MAX_TOKENS, "system": system,
         "messages": [{"role": "user", "content": user}]})
    if err or not d:
        return {"error": err or f"HTTP {status}", "latency_ms": ms}
    txt = "".join(b.get("text", "") for b in (d.get("content") or []) if b.get("type") == "text")
    u = d.get("usage") or {}
    it, ot = u.get("input_tokens", 0), u.get("output_tokens", 0)
    return {"text": txt, "input_tokens": it, "output_tokens": ot, "cost_usd": _direct_cost(model_name, it, ot),
            "latency_ms": ms, "provider": "anthropic-direct", "truncated": d.get("stop_reason") == "max_tokens"}


def _call_openai(model_name, system, user, key):
    status, d, ms, err = _post(
        "https://api.openai.com/v1/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"model": model_name, "max_completion_tokens": MAX_TOKENS,   # newer OpenAI models reject max_tokens
         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    if err or not d:
        return {"error": err or f"HTTP {status}", "latency_ms": ms}
    txt = d["choices"][0]["message"].get("content") or ""
    u = d.get("usage") or {}
    it, ot = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
    return {"text": txt, "input_tokens": it, "output_tokens": ot,
            "cost_usd": _direct_cost(model_name, it, ot), "latency_ms": ms, "provider": "openai-direct"}


def _call_openrouter(model_ref, system, user, key):
    status, d, ms, err = _post(
        "https://openrouter.ai/api/v1/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
         "HTTP-Referer": "https://dataengine.hyndlandpartners.com", "X-Title": "Data Engine model-compare"},
        {"model": model_ref, "max_tokens": MAX_TOKENS,
         "usage": {"include": True},                 # ask OpenRouter to return real cost
         "provider": {"allow_fallbacks": False},     # pin upstream — reproducible benchmark
         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    if err or not d:
        return {"error": err or f"HTTP {status}", "latency_ms": ms}
    txt = d["choices"][0]["message"].get("content") or ""
    u = d.get("usage") or {}
    return {"text": txt, "input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0),
            "cost_usd": u.get("cost"), "latency_ms": ms, "provider": d.get("provider") or "openrouter"}


def call_model(model_ref: str, system: str, user: str, config: dict) -> dict:
    """Dispatch one model call by route. Per agreement: Anthropic and OpenAI ALWAYS use their
    direct APIs (never OpenRouter) — a direct failure is surfaced as-is, not silently rerouted.
    OpenRouter is used ONLY for providers we hold no direct key for (Google, DeepSeek, Kimi, …)."""
    route = resolve_route(model_ref, config)
    name = model_ref.split("/", 1)[1] if "/" in model_ref else model_ref
    if route == "anthropic":
        # OpenRouter spells versions with a dot (claude-opus-4.8); Anthropic's own API wants the
        # native hyphenated id (claude-opus-4-8).
        res = _call_anthropic(name.replace(".", "-"), system, user, config["anthropic_api_key"])
    elif route == "openai":
        res = _call_openai(name, system, user, config["openai_api_key"])
    else:
        k = openrouter_key()
        res = {"error": "no OpenRouter key configured"} if not k else _call_openrouter(model_ref, system, user, k)
    res["model"] = model_ref
    res["route"] = route
    return res


# ── abstention-aware scoring against a per-case expectation spec ────────────────
#   spec = {"mode":"entity","options":[{"name":..,"registry_id":..}, ...]}  → expect one of these
#        | {"mode":"none"}                                                   → expect NO recommendation
# A model that GUESSES an entity when the case expects abstention FAILS — we don't reward a
# confident-but-wrong answer. A model that correctly abstains PASSES.
ABSTAIN_CONF = {"insufficient", "none", "no match", "unknown", ""}


def _norm(s) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _entity_label(rec):
    if not rec:
        return None
    nm = rec.get("legal_entity_name") or "?"
    return nm + (f" (#{rec['registry_id']})" if rec.get("registry_id") else "")


def _opt_matches(rec, opt):
    """opt has 'name' and/or 'registry_id'; match if either (normalised) appears in the entity's
    name+id — tolerant so a registry id or LEI given as either field still matches."""
    if not rec:
        return False
    blob = _norm(rec.get("legal_entity_name")) + "|" + _norm(rec.get("registry_id"))
    for key in ("registry_id", "name"):
        v = _norm(opt.get(key))
        if v and v in blob:
            return True
    return False


def default_spec_from_expect(expect):
    """Fallback expectation when none is defined: expect an entity carrying any expect token."""
    opts = [{"name": e} for e in (expect or []) if str(e).strip()]
    return {"mode": "entity", "options": opts} if opts else None


def score(report, spec) -> dict:
    rec = (report or {}).get("recommended_entity")
    conf = (report or {}).get("confidence")
    abstained = (rec is None) or (str(conf).lower() in ABSTAIN_CONF)
    got = _entity_label(rec)
    if not spec or not spec.get("mode"):
        return {"scored": False, "got": got}
    if spec["mode"] == "none":
        ok = abstained
        return {"scored": True, "ok": ok, "got": got,
                "verdict": "correctly abstained" if ok else "guessed — should have abstained"}
    # mode == "entity"
    if abstained:
        return {"scored": True, "ok": False, "got": got, "verdict": "abstained — an entity was expected"}
    hit = next((o for o in (spec.get("options") or []) if _opt_matches(rec, o)), None)
    return {"scored": True, "ok": bool(hit), "got": got,
            "verdict": "matched expected" if hit else "different entity than expected"}


# ══════════════════════════════════════════════════════════════════════════════
# Cached Phase-1 input — reproduce production phases 2-5, then build_analysis_messages
# ══════════════════════════════════════════════════════════════════════════════
def _domain_of(url: str) -> str:
    from urllib.parse import urlparse
    import re
    return re.sub(r"^www\.", "", (urlparse(url).hostname or ""))


def build_input(config: dict, case: dict, refresh: bool = False, progress=None) -> dict:
    """Return {'system', 'user', 'meta'} — the exact analysis input production would feed the
    LLM for this case. Cached per case id; regenerated only on refresh (expensive: it runs
    fetch/extract/registry against the live toolchain). `progress` (optional) receives the
    pipeline's phase logs so a streaming caller can show live feedback."""
    cid = case.get("id")
    if cid and not refresh:
        cached = _input_get(cid)
        if cached:
            return cached

    agent = EntityLookup(config, progress_callback=progress)
    url = (case.get("url") or "").strip()
    names = case.get("names") or []
    registries: dict = {}

    if url:                                           # URL mode — full pipeline (phases 2-5)
        domain = _domain_of(url)
        website_data = agent.fetch_website_data(url, domain)
        entity_info = agent.extract_entities_with_llm(website_data, registries)
        registries.update(agent.search_registries(entity_info, domain))
        cross = agent.cross_reference_sec_data(website_data, registries, entity_info)
        if cross:
            registries["sec_cross_reference"] = cross
    else:                                             # names mode — search only, no fetch/extract LLM
        url, domain = "", ""
        entity_info = {"entity_names": names, "jurisdiction": case.get("jurisdiction") or "unknown",
                       "addresses": []}
        website_data = {"pages": {}, "whois": "Not available"}
        registries.update(agent.search_registries(entity_info, domain))

    system_prompt, user_message, _sections = agent.build_analysis_messages(
        url, domain, website_data, entity_info, registries)
    meta = {"mode": "url" if case.get("url") else "names",
            "registries": list(registries.keys()),
            "extracted_names": entity_info.get("entity_names"),
            "jurisdiction": entity_info.get("jurisdiction"),
            "user_chars": len(user_message), "system_chars": len(system_prompt)}
    out = {"system": system_prompt, "user": user_message, "meta": meta}
    if cid:
        _input_put(cid, out)
    return out


def spec_for(case):
    """Expectation: explicit override on the case > stored per-case spec > fallback from coverage expects."""
    cid = case.get("id")
    return case.get("expect_spec") or (get_expect(cid) if cid else None) \
        or default_spec_from_expect(case.get("expect"))


def run_one_model(config, inp, model, spec, cid=None) -> dict:
    """Run ONE model on the cached input, score it, cache + return the row. Never raises — a
    failed model becomes an error row so it can't abort a batch/stream (that was the bug that
    killed a whole run when one model choked)."""
    try:
        raw = call_model(model, inp["system"], inp["user"], config)
    except Exception as e:  # noqa: BLE001
        raw = {"model": model, "error": f"{type(e).__name__}: {e}"}
    report = None
    if raw.get("text"):
        try:
            report = agent_parse(config, raw["text"])
        except Exception as e:  # noqa: BLE001
            raw["error"] = raw.get("error") or f"parse: {type(e).__name__}: {e}"
    rec = (report or {}).get("recommended_entity")
    conf = (report or {}).get("confidence")
    sc = score(report, spec)
    row = {
        "model": model, "route": raw.get("route"), "provider": raw.get("provider"),
        "error": raw.get("error"),
        "recommended": rec, "confidence": conf,
        "cost_usd": raw.get("cost_usd"), "latency_ms": raw.get("latency_ms"),
        "input_tokens": raw.get("input_tokens"), "output_tokens": raw.get("output_tokens"),
        "json_ok": bool(rec is not None or (raw.get("text") and not raw.get("error"))),
        "truncated": raw.get("truncated"),
        "score": sc, "raw": (raw.get("text") or "")[:60000],
    }
    if cid:
        _result_put(cid, model, row)
    return row


def run_case(config: dict, case: dict, models: list, refresh_input: bool = False,
             refresh_models: bool = False) -> dict:
    """Build (or reuse) the cached input, then run each model, cache + return per-model results."""
    inp = build_input(config, case, refresh=refresh_input)
    cid = case.get("id")
    spec = spec_for(case)
    results, to_run = [], []
    for m in models:
        if cid and not refresh_models:
            cached = _result_get(cid, m)
            if cached and not cached.get("error"):   # keep successes cached; RETRY failures
                results.append(cached)
                continue
        to_run.append(m)
    if to_run:                                        # models are independent HTTP calls → run in parallel
        with ThreadPoolExecutor(max_workers=min(10, len(to_run))) as ex:
            results.extend(ex.map(lambda m: run_one_model(config, inp, m, spec, cid), to_run))
    total_cost = round(sum((r.get("cost_usd") or 0) for r in results), 4)
    scored = [r for r in results if r["score"].get("scored")]
    return {"case_id": cid, "input_meta": inp["meta"], "spec": spec,
            "summary": {"models": len(results),
                        "scored": len(scored),
                        "pass": sum(1 for r in scored if r["score"].get("ok")),
                        "cost_usd": total_cost},
            "results": results}


_PARSE_AGENT = None


def agent_parse(config, text):
    """Parse a model's output with the SAME parser production uses (fenced/brace/salvage)."""
    global _PARSE_AGENT
    if _PARSE_AGENT is None:
        _PARSE_AGENT = EntityLookup(config, progress_callback=None)
    return _PARSE_AGENT.parse_json_response(text, None)


# ══════════════════════════════════════════════════════════════════════════════
# Schema + cache accessors (Postgres `entity` schema, alongside coverage)
# ══════════════════════════════════════════════════════════════════════════════
def ensure_schema():
    if not coverage.enabled():
        return
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS entity.model_compare_input (
                    case_id      bigint PRIMARY KEY,
                    system_prompt text NOT NULL,
                    user_message  text NOT NULL,
                    meta          jsonb NOT NULL DEFAULT '{}',
                    created_at    timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS entity.model_compare_results (
                    case_id      bigint NOT NULL,
                    model        text   NOT NULL,
                    result       jsonb  NOT NULL,
                    created_at   timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (case_id, model)
                );
                CREATE TABLE IF NOT EXISTS entity.model_compare_expect (
                    case_id      bigint PRIMARY KEY,
                    spec         jsonb  NOT NULL,   -- {"mode":"entity","options":[...]} | {"mode":"none"}
                    created_at   timestamptz NOT NULL DEFAULT now()
                );
            """)
        c.commit()


def get_expect(cid):
    if not cid or not coverage.enabled():
        return None
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT spec FROM entity.model_compare_expect WHERE case_id=%s", (cid,))
            r = cur.fetchone()
            return r["spec"] if r else None


def put_expect(cid, spec):
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.model_compare_expect (case_id, spec) VALUES (%s,%s) "
                "ON CONFLICT (case_id) DO UPDATE SET spec=EXCLUDED.spec, created_at=now()",
                (cid, json.dumps(spec)))
        c.commit()


def _input_get(cid):
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT system_prompt, user_message, meta FROM entity.model_compare_input WHERE case_id=%s", (cid,))
            r = cur.fetchone()
            return {"system": r["system_prompt"], "user": r["user_message"], "meta": r["meta"]} if r else None


def _input_put(cid, out):
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.model_compare_input (case_id, system_prompt, user_message, meta) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (case_id) DO UPDATE SET "
                "system_prompt=EXCLUDED.system_prompt, user_message=EXCLUDED.user_message, "
                "meta=EXCLUDED.meta, created_at=now()",
                (cid, out["system"], out["user"], json.dumps(out["meta"])))
            # the input was (re)generated → any stored model results were run against the OLD
            # input and are now stale. Clear them so they get re-run against this input.
            cur.execute("DELETE FROM entity.model_compare_results WHERE case_id=%s", (cid,))
        c.commit()


def get_cached(cid):
    """Everything the page needs on load for a case: the input meta + all stored model rows."""
    inp = _input_get(cid)
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT result FROM entity.model_compare_results WHERE case_id=%s ORDER BY model", (cid,))
            rows = [r["result"] for r in cur.fetchall()]
    return {"case_id": cid, "input_meta": (inp or {}).get("meta"), "spec": get_expect(cid), "results": rows}


def matrix():
    """Every case + all its cached model results, for the overview grid (cases × models)."""
    out = []
    for c in coverage.list_cases():
        cid = c["id"]
        cached = get_cached(cid)
        out.append({
            "id": cid, "name": c["name"], "url": c.get("url"), "names": c.get("names"),
            "spec": cached.get("spec"), "input_meta": cached.get("input_meta"),
            "results": {r["model"]: r for r in (cached.get("results") or [])},
        })
    return {"cases": out, "models": DEFAULT_MODELS}


def _result_get(cid, model):
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT result FROM entity.model_compare_results WHERE case_id=%s AND model=%s", (cid, model))
            r = cur.fetchone()
            return r["result"] if r else None


def _result_put(cid, model, row):
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.model_compare_results (case_id, model, result) VALUES (%s,%s,%s) "
                "ON CONFLICT (case_id, model) DO UPDATE SET result=EXCLUDED.result, created_at=now()",
                (cid, model, json.dumps(row, default=str)))
        c.commit()
