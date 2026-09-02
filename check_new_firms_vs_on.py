#!/usr/bin/env python3
"""
Which of the PE firms Mergr added since the last pull are NOT already ON buyers?

Pulls the LIVE ON buyer list (via the AWS box -> cPanel ssh hop) rather than the local
replica, which lags — checked 28 Aug 2026, the replica was 3,031 buyers behind and would
have reported firms as missing that ON already holds.

Writes a human-readable triage list; nothing is added to ON automatically.
"""
import json
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
FIRM_DIR = os.path.join(BASE, "mergr_investors")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "new_firms_vs_on.txt")
SINCE_ID = int(sys.argv[2]) if len(sys.argv) > 2 else 7078      # June high-water mark

SUFFIX = (r'(llp|llc|lp|ltd|limited|plc|inc|incorporated|corp|corporation|co|company|sas|sarl|'
          r'sa|srl|spa|ab|as|aps|oy|bv|nv|gmbh|mbh|ag|kg|se|pte|pty|holding|holdings|group|'
          r'capital|partners|management|investments|investment|ventures|equity|private|fund|funds|the)')


def norm(n):
    n = (n or "").lower().replace("&", " and ")
    toks = [t for t in re.sub(r"[^a-z0-9 ]", " ", n).split() if t]
    while toks and re.fullmatch(SUFFIX, toks[-1]):
        toks.pop()
    return "".join(toks)


def dom(w):
    w = (w or "").strip().lower()
    w = re.sub(r"^https?://", "", w)
    w = re.sub(r"^www\.", "", w)
    return w.split("/")[0].split("?")[0]


def live_on_buyers():
    """id, name, website for every buyer in the LIVE ON database."""
    sql = "select id, name, coalesce(website,'') from buyers;"
    b64 = subprocess.run(["base64"], input=sql.encode(), capture_output=True).stdout.decode().replace("\n", "")
    # ON buyer names carry Windows-1252 bytes (0x92 curly apostrophe, e.g. "Crédit Mutuel",
    # "S.à r.l."), which strict UTF-8 decoding rejects — errors="replace" keeps the row rather
    # than killing the run.
    r = subprocess.run(["ssh", "-i", os.path.expanduser("~/.ssh/data-engine-key.pem"),
                        "ec2-user@54.170.119.21", f"/tmp/onq.sh {b64}"],
                       capture_output=True, text=True, errors="replace", timeout=600)
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def main():
    new = []
    for f in os.listdir(FIRM_DIR):
        if not f.endswith(".json"):
            continue
        fid = int(f[:-5])
        if fid <= SINCE_ID:
            continue
        try:
            new.append((fid, json.load(open(os.path.join(FIRM_DIR, f)))))
        except (json.JSONDecodeError, OSError):
            pass
    new.sort()

    on = live_on_buyers()
    on_names = {norm(n) for _, n, _ in on if norm(n)}
    on_doms = {dom(w) for _, _, w in on if dom(w)}

    absent, present = [], []
    for fid, d in new:
        n, w = norm(d.get("name")), dom(d.get("website"))
        hit = (w and w in on_doms) or (n and n in on_names) or \
              any(n and (n in o or (len(o) >= 6 and o in n)) for o in on_names)
        (present if hit else absent).append((fid, d))

    lines = [f"{len(new)} PE firms added by Mergr since firm id {SINCE_ID}",
             f"checked against {len(on):,} LIVE ON buyers",
             f"  {len(absent)} NOT in ON (candidates to add)",
             f"  {len(present)} already in ON", ""]
    lines.append("NOT IN ON — candidates:")
    for fid, d in absent:
        lines.append(f"  [{fid}] {d.get('name','?')}  {dom(d.get('website')) or '(no website)'}"
                     f"  buys={d.get('total_buys') or '-'}  {(d.get('investment_criteria_description') or '')[:110]}")
    lines.append("")
    lines.append("Already in ON:")
    for fid, d in present:
        lines.append(f"  [{fid}] {d.get('name','?')}")
    open(OUT, "w").write("\n".join(lines) + "\n")
    print("\n".join(lines[:4]))
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
