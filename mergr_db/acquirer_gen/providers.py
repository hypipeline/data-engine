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

TIMEOUT = 180

SYSTEM = (
    "You are an M&A research assistant. Given a target company profile, identify likely ACQUIRERS "
    "and relevant PRECEDENT TRANSACTIONS in the same or the most closely adjacent sectors. "
    "Use web search where available to include recent, verifiable deals. "
    "Return ONLY a JSON object (no prose, no markdown fences) matching exactly:\n"
    '{"acquirers":[{"name":"","website":"","type":"","geography":"","rationale":"","source_url":""}],'
    '"deals":[{"acquirer":"","acquirer_website":"","target":"","target_website":"","year":"","value":"","source_url":""}]}\n'
    "Rules: website = the entity's primary domain (e.g. example.com) whenever known — do NOT invent domains; "
    "type = trade/PE/infra/strategic etc; value = deal value with currency if disclosed, else empty; "
    "source_url = a citation URL when available. Prefer real, verifiable entities and deals."
)


def build_user(target, n_acq, n_deals):
    return (f"Target company profile: {target}\n\n"
            f"List up to {n_acq} likely acquirers and up to {n_deals} precedent M&A transactions. "
            f"Include each acquirer's website domain wherever possible so it can be cross-referenced.")


def _extract_json(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)          # first { … last }
    if not m:
        return None
    frag = m.group(0)
    for attempt in (frag, re.sub(r",\s*([}\]])", r"\1", frag)):
        try:
            return json.loads(attempt)
        except Exception:                          # noqa: BLE001
            continue
    return None


def _ok(provider, model, text, in_tok, out_tok, web, t0, citations=None):
    parsed = _extract_json(text) or {}
    return {
        "provider": provider, "model": model, "text": text,
        "acquirers": parsed.get("acquirers") or [],
        "deals": parsed.get("deals") or [],
        "citations": citations or [],
        "usage": {"input_tokens": in_tok or 0, "output_tokens": out_tok or 0, "web_searches": web or 0},
        "latency_ms": int((time.time() - t0) * 1000),
        "parse_ok": bool(parsed), "error": None,
    }


def _err(provider, model, detail, t0):
    if isinstance(detail, dict):
        msg = (detail.get("error") or {}).get("message") if isinstance(detail.get("error"), dict) else str(detail.get("error") or detail)
    else:
        msg = str(detail)
    return {
        "provider": provider, "model": model, "text": "", "acquirers": [], "deals": [], "citations": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "web_searches": 0},
        "latency_ms": int((time.time() - t0) * 1000), "parse_ok": False, "error": str(msg)[:400],
    }


def call_anthropic(key, model, target, s):
    t0 = time.time()
    body = {
        "model": model, "max_tokens": s.get("max_tokens", 6000), "system": SYSTEM,
        "messages": [{"role": "user", "content": build_user(target, s.get("n_acq", 40), s.get("n_deals", 40))}],
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
        return _err("anthropic", model, j, t0)
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    u = j.get("usage", {}) or {}
    stu = u.get("server_tool_use") or {}
    web = stu.get("web_search_requests", 0) if isinstance(stu, dict) else 0
    return _ok("anthropic", model, text, u.get("input_tokens", 0), u.get("output_tokens", 0), web, t0)


def call_openai(key, model, target, s):
    t0 = time.time()
    body = {
        "model": model, "instructions": SYSTEM,
        "input": build_user(target, s.get("n_acq", 40), s.get("n_deals", 40)),
        "max_output_tokens": s.get("max_tokens", 6000),
    }
    if s.get("web"):
        body["tools"] = [{"type": "web_search"}]
    if s.get("temperature") is not None:
        body["temperature"] = s["temperature"]
    try:
        r = httpx.post("https://api.openai.com/v1/responses",
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                       json=body, timeout=TIMEOUT)
        j = r.json()
    except Exception as e:                          # noqa: BLE001
        return _err("openai", model, e, t0)
    if r.status_code != 200:
        return _err("openai", model, j, t0)
    text = j.get("output_text") or ""
    if not text:
        for item in j.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        text += c.get("text", "")
    u = j.get("usage", {}) or {}
    web = sum(1 for item in j.get("output", []) if item.get("type") == "web_search_call")
    return _ok("openai", model, text, u.get("input_tokens", 0), u.get("output_tokens", 0), web, t0)


def call_openai_compat(provider, base_url, key, model, target, s, web_native=False):
    t0 = time.time()
    body = {
        "model": model, "max_tokens": s.get("max_tokens", 6000),
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": build_user(target, s.get("n_acq", 40), s.get("n_deals", 40))}],
    }
    if s.get("temperature") is not None:
        body["temperature"] = s["temperature"]
    try:
        r = httpx.post(base_url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                       json=body, timeout=TIMEOUT)
        j = r.json()
    except Exception as e:                          # noqa: BLE001
        return _err(provider, model, e, t0)
    if r.status_code != 200:
        return _err(provider, model, j, t0)
    try:
        text = j["choices"][0]["message"]["content"]
    except Exception:                               # noqa: BLE001
        return _err(provider, model, j, t0)
    u = j.get("usage", {}) or {}
    srcs = j.get("search_results") or j.get("citations") or []
    cites = [x.get("url") if isinstance(x, dict) else x for x in srcs]
    web = 0
    if web_native:
        web = u.get("num_search_queries") or (1 if srcs else 0)
    return _ok(provider, model, text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), web, t0, cites)


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
        return call_openai_compat("deepseek", "https://api.deepseek.com/chat/completions",
                                  key, model, target, settings, web_native=False)
    return _err(provider, model, f"unknown provider {provider}", time.time())
