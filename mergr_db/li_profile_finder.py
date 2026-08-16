"""LinkedIn Profile Finder — given a LinkedIn *company* URL, find its people.

Local-only tool for the Data Engine. Uses Bright Data's pre-collected LinkedIn people
profiles dataset (gd_l1viktl72bvl7bjuj0) via the Filter API, keyed on
`current_company_company_id`. Each stage is a discrete call so the UI can inspect it:

  parse_company_id(url) -> slug
  submit_filter(slug, titles=?) -> snapshot_id        (builds the snapshot; free)
  snapshot_status(sid) -> {status, count, cost, ...}   (FREE metadata read — gives the count)
  download(sid) -> [people]                             (PAID — pull the records)
  classify_people(rows) -> rows + seniority

Pricing note: Bright Data's per-record rate for the filter path is *stated* ($0.0025/rec on
the marketplace) but not independently verified here — treat est_cost as indicative.
"""
import os
import re
import json
import urllib.request
import urllib.error

import title_seniority

DATASET_ID = "gd_l1viktl72bvl7bjuj0"                       # LinkedIn people profiles
BASE = "https://api.brightdata.com"
EST_RATE = float(os.environ.get("BRIGHTDATA_EST_RATE", "0.0025"))   # $/record (stated, unverified)

# Decision-maker title nets for the "narrow at ingest" scope (coarse, high-recall — cleaned
# afterwards by title_seniority). Kept per audience because PE vs corporate differ.
TITLE_NETS = {
    "pe":        ["Partner", "Principal", "Managing Director", "Managing Partner", "Chief",
                  "Founder", "President", "Chairman", "Head of"],
    "corporate": ["Chief", "CEO", "CFO", "President", "VP", "Vice President", "Head of",
                  "Managing Director", "Corporate Development", "M&A", "Corporate Finance"],
}


def _token():
    t = os.environ.get("BRIGHTDATA_TOKEN")
    if not t:
        raise RuntimeError("BRIGHTDATA_TOKEN not set")
    return t


def _req(method, path, body=None):
    url = path if path.startswith("http") else BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Authorization": "Bearer " + _token(),
                                        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
    try:
        return json.loads(raw)
    except Exception:
        # download endpoint returns a JSON array or JSONL
        try:
            return [json.loads(l) for l in raw.splitlines() if l.strip()]
        except Exception:
            return {"_raw": raw}


# ---- stage 1: parse -------------------------------------------------------
def parse_company_id(url: str) -> str:
    """LinkedIn company URL -> current_company_company_id (the slug)."""
    u = (url or "").strip()
    m = re.search(r"/company/([^/?#]+)", u)
    if m:
        return m.group(1).lower()
    m = re.search(r"/organization-guest/company/([^/?#]+)", u)
    if m:
        return m.group(1).lower()
    # bare slug fallback
    return re.sub(r"[^a-z0-9\-]", "", u.lower())


# ---- filter builder -------------------------------------------------------
def _title_or_group(titles):
    """OR-group of `position includes <title>`, nested into sub-groups of <=4 (API limit)."""
    rules = [{"name": "position", "operator": "includes", "value": t} for t in titles]
    if len(rules) <= 4:
        return {"operator": "or", "filters": rules}
    chunks = [rules[i:i + 4] for i in range(0, len(rules), 4)]   # nested OR of OR-groups
    return {"operator": "or", "filters": [{"operator": "or", "filters": c} for c in chunks]}


def build_filter(company_id: str, titles=None, min_followers=0):
    """Filter for a company's people. `min_followers` biases the pull toward real, established
    profiles *at ingest* — the Filter API has no server-side sort, so with a records_limit cap the
    only way to get the best N (not an arbitrary N) is to filter out low-signal accounts first."""
    rules = [{"name": "current_company_company_id", "operator": "=", "value": company_id}]
    if titles:
        rules.append(_title_or_group(titles))
    if min_followers and min_followers > 0:
        rules.append({"name": "followers", "operator": ">=", "value": int(min_followers)})
    return rules[0] if len(rules) == 1 else {"operator": "and", "filters": rules}


# ---- stage 2: submit + status (FREE count) --------------------------------
def submit_filter(company_id: str, titles=None, records_limit=None, min_followers=0):
    body = {"dataset_id": DATASET_ID, "filter": build_filter(company_id, titles, min_followers)}
    if records_limit:
        body["records_limit"] = records_limit
    resp = _req("POST", "/datasets/filter", body)
    return {"snapshot_id": resp.get("snapshot_id"), "filter": body["filter"], "error": resp.get("validation_errors") or resp.get("error")}


def snapshot_status(sid: str):
    m = _req("GET", f"/datasets/snapshots/{sid}")
    count = m.get("dataset_size")
    return {
        "status": m.get("status"),                # building | ready | failed
        "count": count,
        "file_size": m.get("file_size"),
        "reported_cost": m.get("cost"),
        "est_cost": round((count or 0) * EST_RATE, 2),
        "est_rate": EST_RATE,
        "error": m.get("error"),
    }


# ---- stage 3: download (PAID) + classify ----------------------------------
def download(sid: str):
    d = _req("GET", f"/datasets/snapshots/{sid}/download?format=json")
    if isinstance(d, dict) and d.get("_raw") is not None:
        return None          # not ready yet / delivery job building
    return d if isinstance(d, list) else [d]


def _quality(r):
    """Credibility signals — a proxy for 'is this a real, established person vs a wannabe who just
    listed the company'. followers is the strongest single signal; profile completeness + a real
    photo + recommendations reinforce it. Returns (score, followers, connections, recommendations)."""
    import math
    f = r.get("followers") or 0
    c = r.get("connections") or 0
    recs = r.get("recommendations_count") or 0
    complete = sum(1 for k in ("about", "education", "certifications", "experience") if r.get(k))
    photo = 0 if r.get("default_avatar") else 1           # default_avatar True = generic silhouette
    score = round(math.log10(f + 1) * 10 + math.log10(c + 1) * 5 + recs * 4 + complete * 3 + photo * 3, 1)
    return score, f, c, recs


def classify_people(records):
    out = []
    for r in records or []:
        pos = r.get("position")
        c = title_seniority.classify(pos)
        q, followers, connections, recs = _quality(r)
        out.append({
            "name": r.get("name"),
            "position": pos,
            "level": c["level"],
            "decision_maker": c["decision_maker"],
            "url": r.get("url"),
            "location": r.get("location"),
            "current_company_name": r.get("current_company_name"),
            "followers": followers,
            "connections": connections,
            "recommendations": recs,
            "quality": q,
        })
    # default: highest credibility first — buries "pretend CEO" profiles with ~0 followers
    out.sort(key=lambda x: -x["quality"])
    return out
