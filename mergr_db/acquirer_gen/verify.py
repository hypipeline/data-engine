"""
Grounding layer — cross-check generated acquirers against your OWN data (free, no web/API cost):
  • ON buyers      (buyer_match.buyers — by website domain, contact email-domains, or normalized name)
  • Mergr firms    (public.firms)
  • Mergr companies(public.companies)
  • buyer_mergr    (marks a firm/company that is ALSO one of your ON buyers)

Match precedence: exact domain first (high precision), then normalized-name. ON matches win ties.
Build the index once (build_index) and reuse across many acquirers / models.
"""
import re

import psycopg2

SUFFIXES = {"ltd", "limited", "llc", "plc", "inc", "incorporated", "lp", "llp", "group", "holdings",
            "holding", "partners", "capital", "management", "co", "corp", "corporation", "company",
            "the", "sa", "ag", "gmbh", "bv", "pty", "sarl", "spa", "srl", "as", "oy", "ab"}


def norm_name(n):
    if not n:
        return ""
    n = re.sub(r"[^a-z0-9 ]", " ", str(n).lower())
    toks = [t for t in n.split() if t and t not in SUFFIXES]
    return " ".join(toks)


def domain_of(url):
    if not url:
        return ""
    u = re.sub(r"^https?://", "", str(url).lower().strip())
    u = re.sub(r"^www\.", "", u)
    return u.split("/")[0].split("?")[0].strip()


def build_index(conn):
    """One pass over buyers/firms/companies → {domain->rec} and {norm_name->rec}. ON wins on ties."""
    idx_domain, idx_name = {}, {}

    def add(rec):
        kind, _id, name, website, is_on, on_id = rec
        d = domain_of(website)
        if d:
            idx_domain.setdefault(d, rec)
        nn = norm_name(name)
        if nn:
            idx_name.setdefault(nn, rec)

    cur = conn.cursor()
    # ON buyers first, so setdefault keeps them over Mergr on collisions
    cur.execute("SELECT id, name, website, email_domains FROM buyer_match.buyers")
    for id_, name, web, edoms in cur.fetchall():
        rec = ("on", id_, name, web, True, id_)
        add(rec)
        for d in (edoms or []):
            if d:
                idx_domain.setdefault(d, rec)
    # which firm/company ids are themselves ON buyers
    cur.execute("SELECT firm_id, company_id, buyer_id FROM buyer_match.buyer_mergr")
    on_firm, on_co = {}, {}
    for fid, cid, bid in cur.fetchall():
        if fid:
            on_firm[fid] = bid
        if cid:
            on_co[cid] = bid
    cur.execute("SELECT firm_id, name, website, domain FROM firms")
    for fid, name, web, dom in cur.fetchall():
        add(("firm", fid, name, web or ("http://" + dom if dom else None), fid in on_firm, on_firm.get(fid)))
    cur.execute("SELECT company_id, name, website, domain FROM companies")
    for cid, name, web, dom in cur.fetchall():
        add(("company", cid, name, web or ("http://" + dom if dom else None), cid in on_co, on_co.get(cid)))
    return idx_domain, idx_name


def _mk(rec, how):
    kind, _id, name, web, is_on, on_id = rec
    status = "in_on" if is_on else ("in_mergr" if kind in ("firm", "company") else "in_on")
    return {"status": status, "kind": kind, "match_id": _id, "match_name": name,
            "match_website": web, "on_buyer_id": on_id if is_on else None, "matched_by": how}


def match_one(index, name, website):
    idx_domain, idx_name = index
    d = domain_of(website)
    if d and d in idx_domain:
        return _mk(idx_domain[d], "domain")
    nn = norm_name(name)
    if nn and nn in idx_name:
        return _mk(idx_name[nn], "name")
    return {"status": "none", "kind": None, "match_id": None, "match_name": None,
            "match_website": None, "on_buyer_id": None, "matched_by": None}


def match_acquirers(index, acquirers):
    out = []
    for a in acquirers:
        aa = dict(a)
        aa["verify"] = match_one(index, a.get("name"), a.get("website"))
        out.append(aa)
    return out


def counts(acquirers):
    st = [a.get("verify", {}).get("status") for a in acquirers]
    return {"total": len(acquirers), "in_on": st.count("in_on"),
            "in_mergr": st.count("in_mergr"), "net_new": st.count("none")}


def _as_list(v, cap=10, maxlen=42):
    """Coerce a text/array field to a short, de-duped list of clean tokens for chip display."""
    if not v:
        return []
    items = v if isinstance(v, list) else re.split(r"[|,;/\n]+", str(v))
    out = []
    for it in items:
        s = re.sub(r"\s+", " ", str(it)).strip().strip("-•· ")
        if s and len(s) <= maxlen and s.lower() not in {o.lower() for o in out}:
            out.append(s)
        if len(out) >= cap:
            break
    return out


def enrich_buyers(conn, buyers):
    """Attach `facts` (employees + Mergr/ON firm facts) to each grounded buyer — one small query per
    source, keyed on the ids we actually matched. Never overwrites the AI-generated core fields."""
    on_ids, firm_ids, co_ids = set(), set(), set()
    for b in buyers:
        v = b.get("verify") or {}
        if v.get("on_buyer_id"):
            on_ids.add(v["on_buyer_id"])
        if v.get("kind") == "firm" and v.get("match_id"):
            firm_ids.add(v["match_id"])
        if v.get("kind") == "company" and v.get("match_id"):
            co_ids.add(v["match_id"])

    on_f, firm_f, co_f = {}, {}, {}
    cur = conn.cursor()
    if on_ids:
        cur.execute("SELECT id, no_of_employees, is_specialist, sector_keywords, tags "
                    "FROM buyer_match.buyers WHERE id = ANY(%s)", (list(on_ids),))
        for id_, emp, spec, kws, tags in cur.fetchall():
            on_f[id_] = {"employees": emp, "specialist": spec,
                         "sectors": _as_list(kws) or _as_list(tags)}
    if firm_ids:
        cur.execute("SELECT firm_id, pe_assets, size_category, total_buys, largest_buy, "
                    "geographic_preferences, sectors_of_interest, specialist_generalist, investor_type "
                    "FROM firms WHERE firm_id = ANY(%s)", (list(firm_ids),))
        for fid, aum, size, buys, largest, geos, secs, spec, itype in cur.fetchall():
            sp = None
            if spec:
                sp = "special" in str(spec).lower()
            firm_f[fid] = {"aum": aum, "size": size, "acquisitions": buys, "largest_buy": largest,
                           "geos": _as_list(geos), "sectors": _as_list(secs),
                           "specialist": sp, "investor_type": itype}
    if co_ids:
        cur.execute("SELECT company_id, sector, description, investor_count "
                    "FROM companies WHERE company_id = ANY(%s)", (list(co_ids),))
        for cid, sec, desc, inv in cur.fetchall():
            co_f[cid] = {"sectors": _as_list(sec), "mergr_desc": desc, "investor_count": inv}

    def _merge(dst, src):
        for k, val in (src or {}).items():
            if val in (None, [], "") or dst.get(k) not in (None, [], ""):
                continue
            dst[k] = val

    for b in buyers:
        v = b.get("verify") or {}
        f = {}
        if v.get("kind") == "firm":
            _merge(f, firm_f.get(v.get("match_id")))
        if v.get("kind") == "company":
            _merge(f, co_f.get(v.get("match_id")))
        _merge(f, on_f.get(v.get("on_buyer_id")))   # ON employees/specialist fill gaps
        b["facts"] = f
    return buyers
