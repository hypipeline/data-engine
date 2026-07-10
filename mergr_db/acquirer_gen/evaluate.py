#!/usr/bin/env python3
"""
Model × cost × verified-hits bake-off. Runs the SAME target across a matrix of providers/models/
settings, grounds each against ON/Mergr (built once), and prints a comparison table + totals.

  python -m acquirer_gen.evaluate "UK drainage services business for sale, ~£3m EBITDA, reactive commercial"
  python -m acquirer_gen.evaluate "<target>" --acq 40 --deals 40 --searches 15

Keys via env (ANTHROPIC/OPENAI/PERPLEXITY/DEEPSEEK_API_KEY). Postgres via DATABASE_URL.
"""
import sys

import psycopg2

from acquirer_gen import generate as gen
from acquirer_gen import verify

# (provider, model, {web}) — Claude dropped (too expensive at web-search input-token inflation).
MATRIX = [
    ("openai",     "gpt-4o",              {"web": True}),
    ("perplexity", "sonar",               {"web": True}),
    ("perplexity", "sonar-pro",           {"web": True}),
    ("perplexity", "sonar-deep-research", {"web": True}),
    ("deepseek",   "deepseek-chat",       {"web": False}),
]


def main():
    args = sys.argv[1:]
    if not args:
        print("usage: python -m acquirer_gen.evaluate \"<target profile>\" [--acq N --deals N --searches N]")
        sys.exit(1)
    target = args[0]
    split = "--split" in args
    only = args[args.index("--models") + 1].split(",") if "--models" in args else None
    opt = {"n_acq": 100, "n_deals": 100, "max_searches": 20, "max_tokens": 16000}
    for flag, key in (("--acq", "n_acq"), ("--deals", "n_deals"),
                      ("--searches", "max_searches"), ("--tokens", "max_tokens")):
        if flag in args:
            opt[key] = int(args[args.index(flag) + 1])

    conn = psycopg2.connect(gen.PG_DSN)
    print("building ON/Mergr index…", flush=True)
    index = verify.build_index(conn)
    idx_d, idx_n = index
    print(f"  index: {len(idx_d):,} domains · {len(idx_n):,} names\n", flush=True)
    print(f"TARGET: {target}\n", flush=True)

    hdr = f"{'provider/model':28} {'web':4} {'cand':>4} {'ON':>4} {'Mergr':>5} {'new':>4} {'deals':>5} {'srch':>4} {'cost$':>8} {'sec':>5} {'parse'}"
    print(hdr)
    print("-" * len(hdr))
    total_cost = 0.0
    if split:
        print("(split mode: separate ACQUIRERS + DEALS calls per model)\n")
    for provider, model, s in MATRIX:
        if only and model not in only:
            continue
        settings = dict(s, **opt)
        res = (gen.generate_split if split else gen.generate)(conn, target, provider, model, settings, index=index)
        c = res["counts"]
        total_cost += res.get("cost_usd", 0)
        label = f"{provider}/{res.get('model', model)}"[:28]
        if res.get("error"):
            print(f"{label:28} ERROR: {res['error'][:70]}")
            continue
        print(f"{label:28} {('yes' if s.get('web') else 'no'):4} {c['total']:>4} {c['in_on']:>4} "
              f"{c['in_mergr']:>5} {c['net_new']:>4} {c['deals']:>5} {res['usage']['web_searches']:>4} "
              f"{res.get('cost_usd', 0):>8.4f} {res['latency_ms']/1000:>5.1f} {'ok' if res['parse_ok'] else 'FAIL'}")
    print("-" * len(hdr))
    print(f"total spend this run: ${total_cost:.4f}")
    conn.close()


if __name__ == "__main__":
    main()
