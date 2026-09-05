"""
TDC — the firms we watch for leads.

Seeded from the ON advisory_firms roster: name, website and is_active only. Notes
and contact history are relationship data and are deliberately never read.
"""
import difflib
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from psycopg2.extras import RealDictCursor

ENTITY_API = os.environ.get("TDC_ENTITY_API", "http://entity:8000")


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()


def name_match(firm, slug):
    """Firm name against the resolved slug. The Finder always answers, so this is
    what decides whether the answer is worth believing."""
    a = " ".join(_norm(firm))
    b = " ".join(_norm((slug or "").replace("-", " ")))
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 2)


def find_linkedin(website):
    q = urllib.parse.quote(website, safe="")
    req = urllib.request.Request(f"{ENTITY_API}/api/linkedin?q={q}")
    with urllib.request.urlopen(req, timeout=240) as r:
        d = json.loads(r.read())
    url = d.get("linkedin_url") or d.get("url") or ""
    return url or None, d.get("employees") or d.get("employee_count")


def upsert(conn, name, website, linkedin_url, employees, origin, resolved_by, match):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO tdc.coverage
              (name, website, linkedin_url, employees, origin, resolved_by,
               name_match, needs_check)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (name) DO UPDATE SET
              website=EXCLUDED.website, linkedin_url=EXCLUDED.linkedin_url,
              employees=EXCLUDED.employees, resolved_by=EXCLUDED.resolved_by,
              name_match=EXCLUDED.name_match, needs_check=EXCLUDED.needs_check,
              updated_at=now()
        """, (name, website, linkedin_url, employees, origin, resolved_by, match,
              (match or 0) < 0.7))
    conn.commit()


# Views onto the list, keyed rather than passed as SQL. Each is a question someone
# actually asks while working through it, not a column exposed for its own sake.
FILTERS = [
    ("",      "All",                None),
    ("ready", "Ready to watch",     "(bridge IS DISTINCT FROM \'neither\' OR deals_url IS NOT NULL)"),
    ("tx",    "Transactions page",  "deals_url IS NOT NULL"),
    ("li",    "Proven LinkedIn",    "bridge IN (\'both\',\'site_only\',\'linkedin_only\')"),
    ("check", "Needs a look",       "bridge = \'neither\'"),
    ("todo",  "Not reviewed",       "NOT deals_locked"),
]
_CLAUSE = {k: c for k, _, c in FILTERS}


def filter_counts(conn):
    """Every filter's count in one pass, so the links can carry them."""
    parts = [f'count(*) FILTER (WHERE {c}) AS "{k}"' for k, _, c in FILTERS if c]
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f'SELECT count(*) AS "_all", {", ".join(parts)} '
                    f'FROM tdc.coverage WHERE active')
        row = cur.fetchone()
    row[""] = row["_all"]          # the All view is keyed on an empty string
    return row


def rows(conn, include_inactive=False, filt=None):
    """filt is a key from FILTERS; anything unrecognised shows everything."""
    sql = """SELECT id, name, website, linkedin_url, employees, name_match,
                    needs_check, active, resolved_by, bridge, bridge_note,
                    site_links_linkedin, linkedin_lists_site, checked_at,
                    deals_url, deals_how, deals_label, deals_signals, deals_checked_at,
                    deals_locked, deals_set_by, deals_set_at
             FROM tdc.coverage {} ORDER BY needs_check DESC, bridge, name"""
    clauses = [] if include_inactive else ["active"]
    extra = _CLAUSE.get(filt or "")
    if extra:
        clauses.append(extra)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql.format(where))
        return cur.fetchall()


# --------------------------------------------------------------- validation
# Not discovery. The pairing already exists; this asks whether it is true, in the
# only way that is evidence rather than resemblance — does each end point at the
# other? The two directions are independent and are kept apart, because a firm
# that links its LinkedIn but has no website listed on LinkedIn is a different
# situation from one where neither holds.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# A company slug may contain an ampersand (brown-gibbons-lang-&-company). Allow it,
# but never where it opens a query parameter, or Google's &sa=/&ved= tracking lands
# inside the slug. Excluding & outright is what truncated BGL to a URL that 404s.
LINKEDIN_RE = r"linkedin\.com/(?:company|showcase)/((?:[A-Za-z0-9_\-.%]|&(?![A-Za-z]+=))+)"


def _fetch(url, timeout=12):
    if not url.startswith("http"):
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # Unescaped here so every caller sees raw &, not &amp;.
        return html.unescape(r.read().decode("utf-8", "replace"))


def slug_from_site(website):
    """The firm's own footer. Authoritative when present, and free."""
    m = re.search(LINKEDIN_RE, _fetch(website), re.I)
    return m.group(1).rstrip("/") if m else None


def page_live(linkedin_url):
    """True / False / None — None meaning we could not tell, which is not the same
    as dead and must not be recorded as if it were."""
    try:
        _fetch(linkedin_url, 15)
        return True
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except Exception:
        return None


def resolve(website):
    """Finder proposes, the firm's own site repairs, liveness decides.

    Google's result HTML truncates slugs at an ampersand — the character is simply
    absent from the SERP — so the finder cannot recover those however good its regex
    is. When its answer is dead, the firm's own footer is asked instead, and that is
    the better authority anyway: a company knows its own LinkedIn page.

    Returns (url, employees, how). `how` is never a guess dressed as a fact.
    """
    url = emp = None
    try:
        url, emp = find_linkedin(website)
    except Exception:
        pass

    if url and page_live(url) is False:
        try:
            slug = slug_from_site(website)
        except Exception:
            slug = None
        if slug:
            repaired = "https://www.linkedin.com/company/" + slug
            if page_live(repaired) is not False:
                return repaired, emp, "site-repair"
        return url, emp, "finder-dead"

    if url:
        return url, emp, "finder"

    try:
        slug = slug_from_site(website)
    except Exception:
        slug = None
    if slug:
        return "https://www.linkedin.com/company/" + slug, None, "site"
    return None, None, "unresolved"


def _slug(linkedin_url):
    m = re.search(r"/company/([^/?#]+)", linkedin_url or "")
    return m.group(1).rstrip("/").lower() if m else None


def _domain(website):
    d = re.sub(r"^https?://", "", (website or "").strip().lower())
    d = d.split("/")[0].split("?")[0]
    return re.sub(r"^www\.", "", d)


def site_links_linkedin(website, linkedin_url):
    """Direction one: the firm's own site points at that LinkedIn page."""
    want = _slug(linkedin_url)
    if not want:
        return None
    body = _fetch(website)
    for found in re.findall(LINKEDIN_RE, body, re.I):
        if found.rstrip("/").lower() == want:
            return True
    return False


def linkedin_lists_site(website, linkedin_url):
    """Direction two: the LinkedIn page carries that domain. Checked against the
    whole page rather than a parsed website field — the guest view does not always
    render one — so this is 'the domain appears', which is weaker but still a fact
    about the page and never a guess about the name."""
    dom = _domain(website)
    if not dom:
        return None
    body = _fetch(linkedin_url, timeout=15)
    return bool(re.search(re.escape(dom), body, re.I))


def validate(conn, row):
    site, li = row.get("website"), row.get("linkedin_url")
    a = b = None
    notes = []
    if site and li:
        try:
            a = site_links_linkedin(site, li)
        except Exception as e:
            notes.append("site " + type(e).__name__)
        try:
            b = linkedin_lists_site(site, li)
        except Exception as e:
            notes.append("linkedin " + type(e).__name__)

    if a and b:
        verdict = "both"
    elif a:
        verdict = "site_only"
    elif b:
        verdict = "linkedin_only"
    elif a is None and b is None:
        verdict = "unreachable"
    else:
        verdict = "neither"

    with conn.cursor() as cur:
        cur.execute("""UPDATE tdc.coverage
                          SET site_links_linkedin=%s, linkedin_lists_site=%s, bridge=%s,
                              bridge_note=%s, checked_at=now(),
                              needs_check = (%s IN ('neither','unreachable')),
                              updated_at=now()
                        WHERE id=%s""",
                    (a, b, verdict, "; ".join(notes) or None, verdict, row["id"]))
    conn.commit()
    return verdict, a, b, "; ".join(notes)


# --------------------------------------------------------- the transactions page
# Nearly every corporate finance firm publishes its own completed deals, under a
# dozen different names. The page is found by reading the site's own navigation —
# the firm's word for it is better than any list of paths we could invent — and
# only then confirmed by what the page actually contains. A URL called /deals that
# holds no deals is not a deals page.
NAV_WORDS = [
    ("transaction", 10), ("tombstone", 10), ("track record", 9), ("our deals", 9),
    ("recent deals", 9), ("deal news", 8), ("completed", 7), ("credentials", 6),
    ("case stud", 5), ("deals", 5), ("experience", 3), ("portfolio", 3), ("news", 2),
]
DEAL_SIGNAL = re.compile(
    r"\b(advised|advises|acquisition of|acquired by|acquired|sale of|sold to|"
    r"merger with|has been sold|investment in|disposal|exit|MBO|management buyout)\b", re.I)


def _links(base, body):
    out = []
    for m in re.finditer(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', body, re.I | re.S):
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        out.append((urllib.parse.urljoin(base, href), text))
    return out


def _score(url, text):
    hay = f"{text.lower()} {urllib.parse.urlparse(url).path.lower()}"
    return max((w for word, w in NAV_WORDS if word in hay), default=0)


def find_deals_page(website, max_try=4):
    """Returns (url, how, label, signals) — signals being how many deal-shaped
    phrases the page actually carries, so a promising URL that turns out to be a
    marketing page is not recorded as a transactions page."""
    base = website if website.startswith("http") else "https://" + website
    try:
        home = _fetch(base, 15)
    except Exception:
        return None, None, None, None

    host = urllib.parse.urlparse(base).netloc.replace("www.", "")
    cands = {}
    for url, text in _links(base, home):
        if urllib.parse.urlparse(url).netloc.replace("www.", "") != host:
            continue          # a firm's transactions live on its own site
        s = _score(url, text)
        if s and (url not in cands or cands[url][0] < s):
            cands[url] = (s, text)

    def probe(url):
        try:
            body = _fetch(url, 15)
        except Exception:
            return None
        return len(DEAL_SIGNAL.findall(body))

    passing = []
    for url, (sc, text) in sorted(cands.items(), key=lambda kv: -kv[1][0])[:max_try]:
        hits = probe(url)
        if hits and hits >= 3:
            passing.append((url, text, hits))

    # A single deal written up in full carries more deal language than an index
    # listing forty of them, so ranking on hit count alone walks into one story.
    # The index is the shallower URL, so depth decides first and volume only breaks
    # ties — and where a passing page is deep, its parent is tried directly, since
    # a site does not always link its own index from the home page.
    def depth(u):
        return len([p for p in urllib.parse.urlparse(u).path.split("/") if p])

    for url, text, hits in list(passing):
        parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
        if len(parts) >= 2:
            parent = urllib.parse.urljoin(url, "/" + "/".join(parts[:-1]) + "/")
            if parent not in [p[0] for p in passing]:
                ph = probe(parent)
                if ph and ph >= 3:
                    passing.append((parent, text, ph))

    if not passing:
        return None, None, None, 0
    url, text, hits = sorted(passing, key=lambda p: (depth(p[0]), -p[2]))[0]
    return url, "nav", text or None, hits


def save_deals_page(conn, row_id, url, how, label, signals):
    """Scanner write. Locked rows are skipped — a person has already answered."""
    with conn.cursor() as cur:
        cur.execute("""UPDATE tdc.coverage
                          SET deals_url=%s, deals_how=%s, deals_label=%s,
                              deals_signals=%s, deals_checked_at=now(), updated_at=now()
                        WHERE id=%s AND NOT deals_locked""", (url, how, label, signals, row_id))
    conn.commit()


def set_deals_url(conn, row_id, url, actor):
    """Set a firm's transactions page. Whatever is passed is the answer from now on,
    including nothing — the row locks and the scanner leaves it alone thereafter."""
    url = (url or "").strip()
    if url and not url.startswith("http"):
        url = "https://" + url
    signals = None
    if url:
        try:
            signals = len(DEAL_SIGNAL.findall(_fetch(url, 15)))
        except Exception:
            signals = None
    with conn.cursor() as cur:
        cur.execute("""UPDATE tdc.coverage
                          SET deals_url=%s, deals_signals=%s, deals_how='manual',
                              deals_label=NULL, deals_locked=true, deals_set_by=%s,
                              deals_set_at=now(), deals_checked_at=now(), updated_at=now()
                        WHERE id=%s""", (url or None, signals, actor, row_id))
    conn.commit()
    return url or None, signals
