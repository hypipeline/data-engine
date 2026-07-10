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
