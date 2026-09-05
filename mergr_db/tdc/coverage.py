"""
TDC — the firms we watch for leads.

Seeded from the ON advisory_firms roster: name, website and is_active only. Notes
and contact history are relationship data and are deliberately never read.
"""
import difflib
import html
import time
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


# ------------------------------------------------------------------- caching
# TTLs are not uniform, because the pages are not alike. A published LinkedIn post
# and a written-up deal page do not change once they exist, so they are held
# effectively forever. The pages that list them — a company feed, a transactions
# index — change every time something is added, and are the only reason to go back
# to the network at all.
TTL_INDEX = 6 * 3600            # company page, transactions index
TTL_ITEM = 90 * 24 * 3600       # a post or a deal page: immutable in practice


def fetch(conn, url, ttl=TTL_ITEM, timeout=20, force=False):
    """_fetch with the network skipped when we already hold the page."""
    if conn is not None and not force:
        with conn.cursor() as cur:
            cur.execute("""SELECT body FROM tdc.fetch_cache
                            WHERE url = %s
                              AND fetched_at > now() - (%s || ' seconds')::interval""",
                        (url, ttl))
            row = cur.fetchone()
        if row:
            with conn.cursor() as cur:
                cur.execute("UPDATE tdc.fetch_cache SET hits = hits + 1 WHERE url = %s", (url,))
            conn.commit()
            return row[0]

    # Rate limiting lives here rather than in the callers, so it applies on every
    # real request and costs nothing when the page is already held.
    time.sleep(0.4)
    body = _fetch(url, timeout)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO tdc.fetch_cache (url, status, bytes, body)
                           VALUES (%s, 200, %s, %s)
                           ON CONFLICT (url) DO UPDATE SET
                             fetched_at=now(), bytes=EXCLUDED.bytes, body=EXCLUDED.body""",
                        (url, len(body), body))
        conn.commit()
    return body


def cache_stats(conn):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT count(*) AS pages, coalesce(sum(bytes),0) AS bytes,
                              coalesce(sum(hits),0) AS hits,
                              pg_size_pretty(pg_total_relation_size('tdc.fetch_cache')) AS on_disk
                       FROM tdc.fetch_cache""")
        return cur.fetchone()


# ------------------------------------------------------------------ scanning
URN_RE = re.compile(r"urn:li:activity:(\d{15,25})")
DATE_RE = re.compile(r'datePublished"\s*:\s*"([^"]+)"')
SEG_RE = re.compile(r'attributed-text-segment-list__content[^>]*>(.*?)</p>', re.S)
OUT_RE = re.compile(r"https://lnkd\.in/\w+")
OG_RE = re.compile(r'property="og:description"\s+content="([^"]*)"', re.S)
# Chrome and profile furniture, not content.
SKIP_IMG = re.compile(r"static\.licdn|company-background|profile-displayphoto|ghost|"
                      r"spacer|pixel|1x1|logo-|favicon", re.I)


def _post_body(page):
    """The post text, taken from whichever source is longer.

    Neither wins reliably: the rendered segment is usually complete, but one post
    in twenty had og:description at 56 characters against a 1,349-character body,
    and elsewhere og carries paragraph breaks the segment loses. Checked against
    each other across every post scanned, nothing came back truncated.

    Segments also include the post's comments, so only the first is the post — an
    ordering assumption that held for all of them but is worth knowing about.
    """
    seg = SEG_RE.findall(page)
    seg0 = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", seg[0]))).strip() if seg else ""
    m = OG_RE.search(page)
    og = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip() if m else ""
    return (seg0, "segment") if len(seg0) >= len(og) else (og, "og")


def _media(page, base_url, own_host=None):
    """Links and images carried by the page, as evidence rather than decoration."""
    links, seen = [], set()
    for m in re.finditer(r'<a\s[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
                         page, re.I | re.S):
        u = m.group(1)
        host = urllib.parse.urlparse(u).netloc.replace("www.", "").lower()
        if not host or host in seen:
            continue
        if own_host and host == own_host:
            continue
        if re.search(r"linkedin\.com|licdn|twitter|x\.com|facebook|youtube|instagram|"
                     r"google\.|apple\.com|w3\.org", host):
            continue
        seen.add(host)
        txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))).strip()
        links.append({"url": u[:400], "host": host, "text": txt[:120]})
        if len(links) >= 15:
            break

    imgs, iseen = [], set()
    for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', page, re.I):
        src = urllib.parse.urljoin(base_url, m.group(1))
        if SKIP_IMG.search(src) or src in iseen:
            continue
        alt = re.search(r'alt=["\']([^"\']*)["\']', m.group(0))
        iseen.add(src)
        imgs.append({"src": src[:400], "alt": (alt.group(1)[:120] if alt else "")})
        if len(imgs) >= 8:
            break
    return links, imgs


def strip_template(pages, threshold=0.6):
    """Remove what repeats across a site's own pages.

    There is no reliable content boundary in the markup to extract instead. On the
    sites sampled <main> was either absent, 944 bytes of a 143kb page, or the whole
    document; role=main and entry-content matched nothing. So the boundary is found
    by comparison rather than by trusting the site to declare one.

    Template is what every page of a site shares — cookie notices, menus,
    preference panels, footers. Content is what one page has and its siblings do
    not. That is the same rule that separates a firm's real outbound links from its
    FINRA footer, applied to text instead.

    `pages` is a list of line-lists. Needs at least two to say anything; with one
    page there is nothing to compare against and it is returned untouched.
    """
    if len(pages) < 2:
        return pages, [0] * len(pages)
    seen = {}
    for lines in pages:
        for l in set(lines):
            seen[l] = seen.get(l, 0) + 1
    cut = max(2, int(len(pages) * threshold))
    template = {l for l, n in seen.items() if n >= cut}
    out, removed = [], []
    for lines in pages:
        kept = [l for l in lines if l not in template]
        out.append(kept)
        removed.append(sum(len(l) for l in lines if l in template))
    return out, removed


def _plain(h, min_len=2):
    """Readable lines, keeping short ones.

    They used to be dropped below 30 characters, as a stand-in for "not
    navigation" written before there was anything better. It never told a menu
    item from a person: it discarded Home and Contact, and with them DAVID COPP,
    Director, Partner and Team Members — the deal team, which is the point of the
    page. Nav is now removed by comparing a site's pages against each other, which
    is the job the length filter was badly approximating, so the filter goes.
    """
    # Only tags that never hold readable content. nav, header, footer and form are
    # deliberately not here: HTML5 makes header and footer *sectioning* elements,
    # legal inside article and section, so a page can carry one per card. Acuity's
    # deal pages have four of each, and the team members' names sit inside them —
    # stripping by tag name deleted Robbie Allen and David Copp outright. Page
    # chrome is removed by comparing a site's pages instead, which is positional
    # information the tag name does not carry.
    h = re.sub(r"(?is)<(script|style|noscript|svg|template|iframe)\b.*?</\1>", " ", h)
    h = re.sub(r"(?i)</(p|h[1-6]|li|div|br|td|th|span|a)>", "\n", h)
    out = []
    for x in html.unescape(re.sub(r"<[^>]+>", " ", h)).split("\n"):
        t = re.sub(r"\s+", " ", x).strip()
        if len(t) >= min_len and re.search(r"[A-Za-z]", t):
            out.append(t)
    return out


def scan_linkedin(conn, linkedin_url, cap=10):
    """Enumerate activity ids off the company page, then read each post directly.
    Only the enumeration needs a browser user-agent; the posts themselves do not."""
    items = []
    page = fetch(conn, linkedin_url, ttl=TTL_INDEX)      # the feed moves; refetch often
    for urn in sorted(set(URN_RE.findall(page)), reverse=True)[:cap]:
        url = f"https://www.linkedin.com/feed/update/urn:li:activity:{urn}/"
        try:
            b = fetch(conn, url, ttl=TTL_ITEM)           # a published post never changes
        except Exception:
            continue
        t = re.search(r"<title>(.*?)</title>", b, re.S)
        title = re.sub(r"\s+", " ", html.unescape(t.group(1))).strip() if t else ""
        title = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title)
        title = re.sub(r"\s*\|\s*[^|]*posted on the topic.*$", "", title).strip()
        body, body_from = _post_body(b)
        out = OUT_RE.search(body)
        d = DATE_RE.search(b)
        links, imgs = _media(b, url, own_host="linkedin.com")
        items.append({"channel": "linkedin", "external_id": urn, "url": url,
                      "title": title, "body": OUT_RE.sub("", body).strip(),
                      "published_at": d.group(1) if d else None,
                      "expanded": True, "outlink": out.group(0) if out else None,
                      "links": links, "images": imgs, "body_from": body_from,
                      "full_text": body, "full_chars": len(body)})
    return items


def scan_transactions(conn, index_url, cap=12):
    """Read a firm's transactions index.

    Where the index links a page per deal, each is fetched — that is the expanded
    form and carries parties, quotes and rationale. Where it does not, there is
    nothing to click into and the index entries themselves are all there is; those
    are recorded unexpanded rather than skipped, because a title naming both sides
    is still the minimum viable story.
    """
    idx = fetch(conn, index_url, ttl=TTL_INDEX)
    base = urllib.parse.urlparse(index_url)
    root = f"{base.scheme}://{base.netloc}"
    prefix = base.path.rstrip("/")

    targets, seen = [], set()
    for href in re.findall(r'href=["\']([^"\']+)["\']', idx, re.I):
        u = urllib.parse.urljoin(index_url, href)
        p = urllib.parse.urlparse(u)
        if p.netloc.replace("www.", "") != base.netloc.replace("www.", ""):
            continue
        path = p.path.rstrip("/")
        if path.startswith(prefix) and path != prefix and path not in seen:
            seen.add(path); targets.append(u)

    items = []
    if targets:                                    # expanded: a page per deal
        raw = []                                   # collected first, so the template
        for u in targets[:cap]:                    # can be found by comparing them
            slug = urllib.parse.urlparse(u).path.rstrip("/").rsplit("/", 1)[-1]
            try:
                b = fetch(conn, u, ttl=TTL_ITEM)
            except Exception:
                items.append({"channel": "transactions", "external_id": slug, "url": u,
                              "title": slug.replace("-", " "), "body": "",
                              "published_at": None, "expanded": False, "outlink": None,
                              "links": [], "images": [], "body_from": None})
                continue
            t = re.search(r"<title>(.*?)</title>", b, re.S)
            title = re.sub(r"\s+", " ", html.unescape(t.group(1))).strip() if t else slug.replace("-", " ")
            lines, seen = [], set()
            for l in _plain(b):
                if l not in seen:
                    seen.add(l); lines.append(l)
            links, imgs = _media(b, u, own_host=base.netloc.replace("www.", "").lower())
            raw.append((slug, u, title, lines, links, imgs))

        # The index is part of the corpus: it shares the site's furniture, so it
        # helps identify it, and it means a firm with a single deal page still has
        # something to compare against.
        corpus = [r[3] for r in raw] + [_plain(idx)]
        cleaned, removed = strip_template(corpus)
        for (slug, u, title, _l, links, imgs), lines, gone in zip(raw, cleaned, removed):
            full = "\n".join(lines)[:20000]
            items.append({"channel": "transactions", "external_id": slug, "url": u,
                          "title": title, "body": " ".join(lines[0:3])[:1200],
                          "published_at": None, "expanded": True, "outlink": None,
                          "links": links, "images": imgs, "body_from": "page",
                          "full_text": full, "full_chars": len(full),
                          "chrome_chars": gone})
        return items

    # no click targets — take the index entries themselves
    for i, line in enumerate([l for l in _plain(idx) if 25 < len(l) < 300][:cap]):
        items.append({"channel": "transactions", "external_id": f"row-{i}", "url": index_url,
                      "title": line[:180], "body": "", "published_at": None,
                      "expanded": False, "outlink": None,
                      "links": [], "images": [], "body_from": "index"})
    return items


def scan_firm(conn, row):
    """Both channels for one firm. Returns (n_found, n_new, notes)."""
    found, notes = [], []
    if row.get("linkedin_url"):
        try:
            found += scan_linkedin(conn, row["linkedin_url"])
        except Exception as e:
            notes.append(f"LinkedIn: {type(e).__name__}")
    if row.get("deals_url"):
        try:
            found += scan_transactions(conn, row["deals_url"])
        except Exception as e:
            notes.append(f"transactions: {type(e).__name__}")

    new = 0
    with conn.cursor() as cur:
        for it in found:
            cur.execute("""
                INSERT INTO tdc.scan_item
                  (coverage_id, channel, external_id, url, title, body,
                   published_at, expanded, outlink, links, images, body_from,
                   full_text, full_chars, chrome_chars)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (coverage_id, channel, external_id) DO UPDATE SET
                  title=EXCLUDED.title, body=EXCLUDED.body, url=EXCLUDED.url,
                  published_at=EXCLUDED.published_at, expanded=EXCLUDED.expanded,
                  outlink=EXCLUDED.outlink, links=EXCLUDED.links, images=EXCLUDED.images,
                  body_from=EXCLUDED.body_from, full_text=EXCLUDED.full_text,
                  full_chars=EXCLUDED.full_chars, chrome_chars=EXCLUDED.chrome_chars,
                  last_seen=now()
                RETURNING (xmax = 0) AS inserted
            """, (row["id"], it["channel"], it["external_id"], it["url"], it["title"],
                  it["body"], it["published_at"], it["expanded"], it["outlink"],
                  json.dumps(it.get("links") or []), json.dumps(it.get("images") or []),
                  it.get("body_from"), it.get("full_text"), it.get("full_chars"),
                  it.get("chrome_chars")))
            new += 1 if cur.fetchone()[0] else 0
    conn.commit()
    return len(found), new, "; ".join(notes)


def _demote_boilerplate(rows, threshold=0.4):
    """A host linked from most of a firm's pages is chrome, not evidence.

    Two Roads links FINRA, SIPC and its web designer from every page; those arrive
    looking exactly like the one link that matters, schmidt-electric.com on a single
    deal. Frequency is what separates them, and it can only be judged across a
    firm's items rather than within one page.

    Filtered on read rather than on write, so the raw capture is kept and the
    threshold stays a decision rather than a permanent loss.
    """
    per_firm = {}
    for r in rows:
        seen = {l.get("host") for l in (r.get("links") or [])}
        d = per_firm.setdefault(r["coverage_id"], {"n": 0, "hosts": {}})
        d["n"] += 1
        for h in seen:
            d["hosts"][h] = d["hosts"].get(h, 0) + 1

    for r in rows:
        d = per_firm[r["coverage_id"]]
        keep, chrome = [], []
        for l in (r.get("links") or []):
            share = d["hosts"].get(l.get("host"), 0) / max(d["n"], 1)
            (chrome if (d["n"] > 2 and share >= threshold) else keep).append(l)
        r["links"] = keep
        r["chrome_links"] = chrome
    return rows


def scan_firms(conn):
    """Firms that have been scanned, with how much each returned — so the filter
    also answers 'which of these has anything worth reading'.

    The count is n_items, not items: Jinja resolves attributes before keys, so a
    column called `items` on a dict row silently renders the dict's own .items
    method instead of the number.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT c.id, c.name, count(*) AS n_items,
                   count(*) FILTER (WHERE s.channel='linkedin')     AS linkedin,
                   count(*) FILTER (WHERE s.channel='transactions') AS transactions,
                   max(s.last_seen) AS last_scan
            FROM tdc.scan_item s JOIN tdc.coverage c ON c.id = s.coverage_id
            GROUP BY c.id, c.name ORDER BY c.name""")
        return cur.fetchall()


def scan_items(conn, coverage_id=None, channel=None, limit=300):
    clauses, args = [], []
    if coverage_id:
        clauses.append("s.coverage_id = %s"); args.append(coverage_id)
    if channel in ("linkedin", "transactions"):
        clauses.append("s.channel = %s"); args.append(channel)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    args = tuple(args)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            SELECT s.*, c.name AS firm
            FROM tdc.scan_item s JOIN tdc.coverage c ON c.id = s.coverage_id
            {where}
            ORDER BY s.published_at DESC NULLS LAST, s.first_seen DESC
            LIMIT %s""", args)
        return _demote_boilerplate(cur.fetchall())
