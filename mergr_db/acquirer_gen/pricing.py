"""
Approximate USD pricing per model, for cost tracking. Rates are per 1M tokens (input/output) plus a
per-web-search fee. THESE ARE APPROXIMATE — verify against each provider's current dashboard and edit
here; the point is the tracking framework, not exact cents. cost_usd is computed from logged usage.
"""

PRICING = {
    # Anthropic (Claude)
    "claude-opus-4-8":   {"in": 15.0, "out": 75.0, "search": 0.01},
    "claude-sonnet-4-6": {"in": 3.0,  "out": 15.0, "search": 0.01},
    "claude-haiku-4-5":  {"in": 1.0,  "out": 5.0,  "search": 0.01},
    # OpenAI
    "gpt-5":             {"in": 1.25, "out": 10.0, "search": 0.025},
    "gpt-4o":            {"in": 2.5,  "out": 10.0, "search": 0.025},
    "gpt-4.1":           {"in": 2.0,  "out": 8.0,  "search": 0.025},
    # Perplexity Sonar — sonar/sonar-pro bill a FLAT per-request search fee by context tier (NOT per query),
    # so 'search' here is charged once/request (see cost_usd). deep-research genuinely bills per search.
    "sonar":             {"in": 1.0,  "out": 1.0,  "search": 0.012},
    "sonar-pro":         {"in": 3.0,  "out": 15.0, "search": 0.014},
    "sonar-reasoning-pro": {"in": 2.0, "out": 8.0, "search": 0.014},
    "sonar-deep-research": {"in": 2.0, "out": 8.0, "search": 0.005},  # per-search (search-heavy) + reasoning tokens
    # DeepSeek (knowledge-only)
    "deepseek-chat":     {"in": 0.27, "out": 1.10, "search": 0.0},
    "deepseek-reasoner": {"in": 0.55, "out": 2.19, "search": 0.0},
    "deepseek-v4-flash": {"in": 0.10, "out": 0.30, "search": 0.0},
}
DEFAULT = {"in": 1.0, "out": 3.0, "search": 0.01}


def rate(model):
    return PRICING.get(model, DEFAULT)


# Models that bill a FLAT per-request search fee (not per query) — we report many "searches" for depth,
# but they're one billable request. deep-research is the exception (genuinely per-search).
_FLAT_SEARCH = {"sonar", "sonar-pro", "sonar-reasoning-pro"}


def cost_usd(model, usage):
    base = model.replace(" (split)", "")
    p = rate(base)
    searches = usage.get("web_searches", 0)
    if base in _FLAT_SEARCH:
        searches = 1 if searches else 0          # per-request, not per-query
    return round(
        (usage.get("input_tokens", 0) / 1e6) * p["in"]
        + (usage.get("output_tokens", 0) / 1e6) * p["out"]
        + searches * p["search"],
        6,
    )
