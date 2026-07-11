"""
Model-agnostic acquirer/precedent generation across providers, each using its own web search where
available. All return the SAME normalized result dict so they're directly comparable:

  {provider, model, text, acquirers[], deals[], citations[],
   usage:{input_tokens, output_tokens, web_searches}, latency_ms, parse_ok, error}

Web capability: anthropic (web_search tool), openai (Responses web_search), perplexity (native) —
live. deepseek is knowledge-only. Keys read from env (ANTHROPIC/OPENAI/PERPLEXITY/DEEPSEEK_API_KEY).
"""
import json
import os
import re
import time

import httpx

TIMEOUT = 300          # deep-research models run for minutes

SYSTEM = (
    "You are an M&A research assistant. Given a target company profile, identify likely ACQUIRERS "
    "and relevant PRECEDENT TRANSACTIONS in the same or the most closely adjacent sectors. "
    "Use web search where available to include recent, verifiable deals. "
    "Return ONLY a JSON object (no prose, no markdown fences) matching exactly:\n"
    '{"acquirers":[{"name":"","website":"","type":"","geography":"","rationale":"","source_url":""}],'
    '"deals":[{"acquirer":"","acquirer_website":"","target":"","target_website":"","year":"","value":"","source_url":""}]}\n'
    "Rules: website = the entity's primary domain (e.g. example.com) whenever known — do NOT invent domains; "
    "type = trade/PE/infra/strategic etc; value = deal value with currency if disclosed, else empty; "
    "source_url = a citation URL when available. Keep every rationale to a TERSE phrase (max ~8 words) — "
    "prioritise returning MORE items over long explanations. Prefer real, verifiable entities and deals."
)


def build_user(target, n_acq, n_deals):
    return (f"Target company profile: {target}\n\n"
            f"List up to {n_acq} likely acquirers and up to {n_deals} precedent M&A transactions. "
            f"Include each acquirer's website domain wherever possible so it can be cross-referenced.")


# ── Split mode: a focused ACQUIRERS-only and DEALS-only prompt, each fitting a smaller token budget
# (so nothing truncates — esp. DeepSeek's ~8k ceiling) and getting the model's full attention. ──
SYSTEM_ACQ = (
    "You are an M&A research assistant. Given a target company profile, identify the most likely "
    "ACQUIRERS (trade buyers, PE platforms/funds, infrastructure funds, strategics). Use web search "
    "where available. Return ONLY a JSON object (no prose, no markdown fences):\n"
    '{"acquirers":[{"name":"","website":"","type":"","geography":"","rationale":"","source_url":""}]}\n'
    "website = the entity's primary domain (e.g. example.com) when known — do NOT invent domains; "
    "type = trade/PE/infra/strategic; source_url = a citation URL when available. Keep every rationale to a "
    "TERSE phrase (max ~8 words) — prioritise returning MORE acquirers over long explanations. Prefer real entities."
)
SYSTEM_DEALS = (
    "You are an M&A research assistant. Given a target company profile, list relevant PRECEDENT "
    "TRANSACTIONS in the same or the most closely adjacent sectors. Use web search where available to "
    "include recent, verifiable, sourced deals. Return ONLY a JSON object (no prose, no markdown fences):\n"
    '{"deals":[{"acquirer":"","acquirer_website":"","target":"","target_website":"","year":"","value":"","source_url":""}]}\n'
    "value = deal value with currency if disclosed, else empty; website = primary domain when known (do NOT "
    "invent); source_url = a citation URL when available. Prefer real, verifiable deals."
)


def build_user_acq(target, n):
    return (f"Target company profile: {target}\n\nList up to {n} likely acquirers, "
            f"with each acquirer's website domain wherever possible.")


def build_user_deals(target, n):
    return (f"Target company profile: {target}\n\nList up to {n} precedent M&A transactions in this or "
            f"the most closely adjacent sectors, with acquirer/target websites and a source URL wherever possible.")


def _prompt_for(target, s):
    """(system, user) for the requested part — 'acquirers', 'deals', or 'both' (combined, default)."""
    part = s.get("part", "both")
    if part == "acquirers":
        return SYSTEM_ACQ, build_user_acq(target, s.get("n_acq", 40))
    if part == "deals":
        return SYSTEM_DEALS, build_user_deals(target, s.get("n_deals", 40))
    return SYSTEM, build_user(target, s.get("n_acq", 40), s.get("n_deals", 40))


# JSON schema for Perplexity structured output — forces the list instead of a prose research report
# (deep-research otherwise writes an essay and never emits the JSON before hitting max_tokens).
_ACQ_ITEM = {"type": "object", "properties": {k: {"type": "string"} for k in
             ("name", "website", "type", "geography", "rationale", "source_url")}, "required": ["name"]}
_DEAL_ITEM = {"type": "object", "properties": {k: {"type": "string"} for k in
              ("acquirer", "acquirer_website", "target", "target_website", "year", "value", "source_url")},
              "required": ["target"]}


def _schema_for(part):
    props = {}
    if part in ("acquirers", "both"):
        props["acquirers"] = {"type": "array", "items": _ACQ_ITEM}
    if part in ("deals", "both"):
        props["deals"] = {"type": "array", "items": _DEAL_ITEM}
    return {"type": "object", "properties": props, "required": list(props.keys())}


def _salvage(text):
    """Recover as many complete objects as possible from truncated/dirty JSON — for each array
    ("acquirers"/"deals") scan balanced {…} objects and json.load each individually, so a cut-off
    final object is simply skipped rather than failing the whole parse."""
    out = {"acquirers": [], "deals": []}
    for key in ("acquirers", "deals"):
        m = re.search(r'"' + key + r'"\s*:\s*\[', text)
        if not m:
            continue
        region, depth, start = text[m.end():], 0, None
        for j, ch in enumerate(region):
            if ch == "{":
                if depth == 0:
                    start = j
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        out[key].append(json.loads(region[start:j + 1]))
                    except Exception:              # noqa: BLE001
                        pass
                    start = None
            elif ch == "]" and depth == 0:
                break
    return out if (out["acquirers"] or out["deals"]) else None


def _extract_json(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)          # first { … last }
    frag = m.group(0) if m else text
    for attempt in (frag, re.sub(r",\s*([}\]])", r"\1", frag)):
        try:
            return json.loads(attempt)
        except Exception:                          # noqa: BLE001
            continue
    return _salvage(text)                          # truncated output → keep the complete objects


def _ok(provider, model, text, in_tok, out_tok, web, t0, citations=None, prompt=None):
    parsed = _extract_json(text) or {}
    return {
        "provider": provider, "model": model, "text": text,
        "prompt": prompt or {},                       # exact {system, user} sent — for audit
        "acquirers": parsed.get("acquirers") or [],
        "deals": parsed.get("deals") or [],
        "citations": citations or [],
        "usage": {"input_tokens": in_tok or 0, "output_tokens": out_tok or 0, "web_searches": web or 0},
        "latency_ms": int((time.time() - t0) * 1000),
        "parse_ok": bool(parsed), "error": None,
    }


def _err(provider, model, detail, t0, status=None):
    if isinstance(detail, dict):
        err = detail.get("error")
        msg = err.get("message") if isinstance(err, dict) else str(err or detail)
    else:
        msg = str(detail)
    if status:
        msg = "HTTP {}: {}".format(status, msg)
    return {
        "provider": provider, "model": model, "text": "", "prompt": {}, "acquirers": [], "deals": [],
        "citations": [], "usage": {"input_tokens": 0, "output_tokens": 0, "web_searches": 0},
        "latency_ms": int((time.time() - t0) * 1000), "parse_ok": False, "error": str(msg)[:500],
    }


def call_anthropic(key, model, target, s):
    t0 = time.time()
    system, user = _prompt_for(target, s)
    body = {
        "model": model, "max_tokens": s.get("max_tokens", 6000), "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if s.get("web"):
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search",
                          "max_uses": s.get("max_searches", 15)}]
    if s.get("temperature") is not None:
        body["temperature"] = s["temperature"]
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages",
                       headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                "content-type": "application/json"}, json=body, timeout=TIMEOUT)
        j = r.json()
    except Exception as e:                          # noqa: BLE001
        return _err("anthropic", model, e, t0)
    if r.status_code != 200:
        return _err("anthropic", model, j, t0, status=r.status_code)
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    u = j.get("usage", {}) or {}
    stu = u.get("server_tool_use") or {}
    web = stu.get("web_search_requests", 0) if isinstance(stu, dict) else 0
    return _ok("anthropic", model, text, u.get("input_tokens", 0), u.get("output_tokens", 0), web, t0, prompt={"system": system, "user": user})


def call_openai(key, model, target, s):
    t0 = time.time()
    # search-native chat models (…-search-preview / …-search-api) search on EVERY query — the fix for
    # vanilla gpt-4o barely searching. Route via chat/completions + web_search_options.
    if "search" in model:
        return call_openai_compat("openai", "https://api.openai.com/v1/chat/completions",
                                  key, model, target, s, web_native=True)
    system, user = _prompt_for(target, s)
    body = {
        "model": model, "instructions": system, "input": user,
        "max_output_tokens": s.get("max_tokens", 6000),
    }
    if s.get("web"):
        body["tools"] = [{"type": "web_search", "search_context_size": s.get("search_context", "high")}]
    if model.startswith(("gpt-5", "o1", "o3", "o4")):      # reasoning models: effort, no temperature
        body["reasoning"] = {"effort": s.get("effort", "medium")}
    elif s.get("temperature") is not None:
        body["temperature"] = s["temperature"]
    try:
        r = httpx.post("https://api.openai.com/v1/responses",
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                       json=body, timeout=TIMEOUT)
        j = r.json()
    except Exception as e:                          # noqa: BLE001
        return _err("openai", model, e, t0)
    if r.status_code != 200:
        return _err("openai", model, j, t0, status=r.status_code)
    text = j.get("output_text") or ""
    if not text:
        for item in j.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        text += c.get("text", "")
    u = j.get("usage", {}) or {}
    web = sum(1 for item in j.get("output", []) if item.get("type") == "web_search_call")
    return _ok("openai", model, text, u.get("input_tokens", 0), u.get("output_tokens", 0), web, t0, prompt={"system": system, "user": user})


def call_openai_compat(provider, base_url, key, model, target, s, web_native=False):
    t0 = time.time()
    system, user = _prompt_for(target, s)
    body = {
        "model": model, "max_tokens": s.get("max_tokens", 6000),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if web_native:                                  # ask Perplexity to search deeper/broader
        body["web_search_options"] = {"search_context_size": s.get("search_context", "high")}
    if "perplexity" in base_url:                    # force JSON output (deep-research writes prose essays otherwise)
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"schema": _schema_for(s.get("part", "both"))}}
    if s.get("temperature") is not None:
        body["temperature"] = s["temperature"]
    try:
        r = httpx.post(base_url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                       json=body, timeout=TIMEOUT)
        j = r.json()
    except Exception as e:                          # noqa: BLE001
        return _err(provider, model, e, t0)
    if r.status_code != 200:
        return _err(provider, model, j, t0, status=r.status_code)
    try:
        text = j["choices"][0]["message"]["content"]
    except Exception:                               # noqa: BLE001
        return _err(provider, model, j, t0)
    u = j.get("usage", {}) or {}
    srcs = j.get("search_results") or j.get("citations") or []
    cites = [x.get("url") if isinstance(x, dict) else x for x in srcs]
    # honest search depth: num_search_queries when given, else sources consulted (citation count)
    web = (u.get("num_search_queries") or len(srcs)) if web_native else 0
    return _ok(provider, model, text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), web, t0, cites, prompt={"system": system, "user": user})


KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
           "perplexity": "PERPLEXITY_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}


def run_provider(provider, model, target, settings):
    key = os.environ.get(KEY_ENV.get(provider, ""))
    if not key:
        return _err(provider, model, f"{KEY_ENV.get(provider)} not set in env", time.time())
    if provider == "anthropic":
        return call_anthropic(key, model, target, settings)
    if provider == "openai":
        return call_openai(key, model, target, settings)
    if provider == "perplexity":
        return call_openai_compat("perplexity", "https://api.perplexity.ai/chat/completions",
                                  key, model, target, settings, web_native=True)
    if provider == "deepseek":
        s = dict(settings, max_tokens=min(settings.get("max_tokens", 6000), 8000))  # DeepSeek output ceiling
        return call_openai_compat("deepseek", "https://api.deepseek.com/chat/completions",
                                  key, model, target, s, web_native=False)
    return _err(provider, model, f"unknown provider {provider}", time.time())


def run_provider_retry(provider, model, target, settings, tries=2):
    """Retry once on a hard error or an empty/failed parse (transient blips, rate limits)."""
    last = None
    for i in range(tries):
        last = run_provider(provider, model, target, settings)
        if not last.get("error") and (last.get("acquirers") or last.get("deals")):
            return last
        if i < tries - 1:
            time.sleep(1.5 * (i + 1))
    return last
