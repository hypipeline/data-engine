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
    # Perplexity Sonar (per-request search fee)
    "sonar":             {"in": 1.0,  "out": 1.0,  "search": 0.005},
    "sonar-pro":         {"in": 3.0,  "out": 15.0, "search": 0.008},
    "sonar-reasoning-pro": {"in": 2.0, "out": 8.0, "search": 0.008},
    # DeepSeek (knowledge-only)
    "deepseek-chat":     {"in": 0.27, "out": 1.10, "search": 0.0},
    "deepseek-reasoner": {"in": 0.55, "out": 2.19, "search": 0.0},
    "deepseek-v4-flash": {"in": 0.10, "out": 0.30, "search": 0.0},
}
DEFAULT = {"in": 1.0, "out": 3.0, "search": 0.01}


def rate(model):
    return PRICING.get(model, DEFAULT)


def cost_usd(model, usage):
    p = rate(model)
    return round(
        (usage.get("input_tokens", 0) / 1e6) * p["in"]
        + (usage.get("output_tokens", 0) / 1e6) * p["out"]
        + usage.get("web_searches", 0) * p["search"],
        6,
    )
