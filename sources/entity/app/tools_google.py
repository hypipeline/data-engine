"""
Entity Lookup v3b (Python) — Google Intelligence / LinkedIn / Yahoo Finance tool cluster.

Faithful like-for-like port of three methods from php/tools.php (class LookupTools):
    googleIntelligence()   -> google_intelligence()
    fetchLinkedInCompany() -> fetch_linkedin_company()
    yahooFinanceData()     -> yahoo_finance_data()  (+ private yahooFormatVal -> _yahoo_format_val)

Composed onto the base ToolBase (toolbase.py) via multiple inheritance. On ``self`` this
mixin uses: self.config, self.api_calls, self._progress(), self.html_to_text() [inherited but
not needed here], and self.log (the append-only tool log, mirroring PHP's private $log array).

stdlib + requests only.
"""
from __future__ import annotations

import html as _htmllib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus, urlparse

import requests


def _php_number_format(number, decimals: int = 0) -> str:
    """Mirror PHP number_format($num, $decimals) — thousands ',' separator, '.' decimal,
    rounding half away from zero."""
    factor = 10 ** decimals
    rounded = math.floor(abs(float(number)) * factor + 0.5) / factor
    if number < 0:
        rounded = -rounded
    return f"{rounded:,.{decimals}f}"


def _extract_ld_org(html: str):
    """First LD+JSON Organization object in the page (handles bare + @graph)."""
    for json_str in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(json_str)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get('@type') == 'Organization':
            return data
        if isinstance(data, dict) and '@graph' in data:
            for item in data['@graph']:
                if isinstance(item, dict) and item.get('@type') == 'Organization':
                    return item
    return None


class GoogleMixin:
    # ── Google Intelligence (Bright Data SERP batch) ──────────────────────────
    def google_intelligence(self, domain: str) -> dict:
        """Google 3 queries about the domain through the Bright Data Web Unlocker (SYNCHRONOUS)
        and return {google_results blob, yahoo_ticker, linkedin_url, site_url}. Uses the Web
        Unlocker (api.brightdata.com/request) — NOT the SERP dataset API, which is now async
        (202 + snapshot polling) and so silently returned empty on every lookup. Mirrors the
        pattern already proven in find_linkedin_url()."""
        result = {'google_results': '', 'yahoo_ticker': None, 'linkedin_url': None, 'site_url': None}
        api_key = self.config.get('brightdata_api_key') or ''
        if not api_key:
            self._progress('google', "Google Intelligence: Bright Data not configured")
            return result

        dom = (domain or '').lower()
        self._progress('google', f"Google Intelligence: searching 3 queries for {domain}...")
        self.count('google', op='intelligence')

        queries = {
            'main': domain,
            'yahoo': f"site:finance.yahoo.com {domain}",
            'linkedin': f"{domain} linkedin",
        }
        with ThreadPoolExecutor(max_workers=3) as ex:
            htmls = dict(zip(queries.keys(),
                             ex.map(lambda q: self._google_serp_html(q), queries.values())))

        # Main query → organic results markdown + canonical site_url (Google's own answer to
        # http/https + www/apex, consumed by resolve_fetch_url Tier 1).
        main_html = htmls.get('main') or ''
        organic = self._parse_serp_organic(main_html)
        self._progress('google', f"Google Intelligence: {len(organic)} organic result(s)")
        if organic:
            google_md = [f"### Google Search Results for {domain}", '']
            for title, link in organic[:10]:
                if result['site_url'] is None:
                    h = (urlparse(link).hostname or '').lower()
                    if h == dom or h.endswith('.' + dom):
                        result['site_url'] = link
                google_md.append(f"- **{title}**")
                google_md.append(f"  {link}")
            result['google_results'] = "\n".join(google_md)

        # Yahoo Finance ticker
        m = re.search(r'finance\.yahoo\.com/quote/([A-Z0-9a-z.\-]+)', htmls.get('yahoo') or '')
        if m:
            result['yahoo_ticker'] = m.group(1)
            self._progress('google', f"Found Yahoo Finance ticker: {m.group(1)}")

        # LinkedIn company page — dedicated query, fall back to the main results
        for src in (htmls.get('linkedin') or '', main_html):
            m = re.search(r'linkedin\.com/company/[A-Za-z0-9_\-.%]+', src, re.I)
            if m:
                result['linkedin_url'] = 'https://www.' + m.group(0).rstrip('.')
                self._progress('google', f"Found LinkedIn: {result['linkedin_url']}")
                break

        self.log.append({
            'tool': 'google_intelligence',
            'input': domain,
            'output':
                "google:" + str(len(result['google_results'])) + " chars, " +
                "yahoo:" + (result['yahoo_ticker'] or 'none') + ", " +
                "linkedin:" + (result['linkedin_url'] or 'none') + ", " +
                "site:" + (result['site_url'] or 'none'),
        })
        return result

    def _google_serp_html(self, query: str, start: int = 0):
        """Fetch a Google results page for `query` via the Bright Data Web Unlocker (synchronous).
        `start` paginates in 10s (start=10 → page 2). Returns raw HTML or None."""
        api_key = self.config.get('brightdata_api_key') or ''
        if not api_key:
            return None
        self.count('brightdata')
        search_url = ('https://www.google.com/search?q=' + quote_plus(query) + '&num=20'
                      + (('&start=' + str(start)) if start else ''))
        payload = {
            'zone': self.config.get('brightdata_zone') or 'web_unlocker1',
            'url': search_url,
            'format': 'raw',
        }
        try:
            r = requests.post(
                'https://api.brightdata.com/request',
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
                data=json.dumps(payload), timeout=90, allow_redirects=False,
            )
            return r.text if r.status_code == 200 else None
        except requests.RequestException:
            return None

    @staticmethod
    def _parse_serp_organic(html: str):
        """Extract (title, url) organic results from a Google SERP HTML page. Each result is an
        <a href="URL"> … <h3>Title</h3>; Google/gstatic-owned links are dropped. The tempered
        dot `(?:(?!</a>).)*?` can't cross an </a>, so there's no catastrophic backtracking."""
        if not html:
            return []
        out, seen = [], set()
        for url, title in re.findall(
                r'<a[^>]*href="(https?://[^"]+)"[^>]*>(?:(?!</a>).)*?<h3[^>]*>(.*?)</h3>',
                html, re.I | re.S):
            if any(bad in url for bad in ('google.', 'gstatic', 'googleusercontent', '/aclk?', 'youtube.com/redirect')):
                continue
            title = _htmllib.unescape(re.sub(r'<[^>]+>', '', title)).strip()
            if not title or url in seen:
                continue
            seen.add(url)
            out.append((title, url))
        return out

    # ── LinkedIn company page (Bright Data Web Unlocker, raw html) ─────────────
    def fetch_linkedin_company(self, linkedin_url: str) -> str:
        api_key = self.config.get('brightdata_api_key') or ''
        if not api_key:
            return ''

        self._progress('linkedin', f"Fetching LinkedIn: {linkedin_url}...")
        self.count('brightdata'); self.count('linkedin', op='company')

        payload = {
            'zone': self.config.get('brightdata_zone') or 'web_unlocker1',
            'url': linkedin_url,
            'format': 'raw',
        }
        try:
            r = requests.post(
                'https://api.brightdata.com/request',
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}',
                },
                data=json.dumps(payload),
                timeout=60,
                allow_redirects=False,
            )
            http_code = r.status_code
            html = r.text
        except requests.RequestException:
            http_code = 0
            html = None

        if http_code != 200 or not html:
            self._progress('linkedin', f"LinkedIn fetch failed (HTTP {http_code})")
            return ''

        # Extract LD+JSON Organization data
        org = None
        matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        if matches:
            for json_str in matches:
                try:
                    data = json.loads(json_str)
                except (ValueError, TypeError):
                    data = None
                if not data:
                    continue
                if isinstance(data, dict) and data.get('@type') == 'Organization':
                    org = data
                    break
                if isinstance(data, dict) and '@graph' in data:
                    found = False
                    for item in data['@graph']:
                        if isinstance(item, dict) and item.get('@type') == 'Organization':
                            org = item
                            found = True
                            break
                    if found:
                        break

        if not org:
            self._progress('linkedin', "LinkedIn: no Organization data found")
            self.log.append({'tool': 'linkedin', 'input': linkedin_url, 'output': 'No LD+JSON'})
            return ''

        # Format as markdown
        md = []
        md.append("### LinkedIn Company Profile")
        md.append(f"Source: {linkedin_url}")
        md.append('')
        if org.get('name'):
            md.append(f"- Name: {org['name']}")
        if org.get('address'):
            addr = org['address']
            parts = [p for p in [
                addr.get('streetAddress') or '',
                addr.get('addressLocality') or '',
                addr.get('postalCode') or '',
                addr.get('addressCountry') or '',
            ] if p]
            md.append("- Address: " + ", ".join(parts))
        num_emp = org.get('numberOfEmployees') or {}
        if isinstance(num_emp, dict) and num_emp.get('value'):
            md.append("- Employees: " + str(num_emp['value']))
        if org.get('sameAs'):
            md.append(f"- Website: {org['sameAs']}")
        if org.get('slogan'):
            md.append(f"- Slogan: {org['slogan']}")
        if org.get('description'):
            desc = org['description']
            md.append('')
            md.append('**Description**')
            md.append(desc[:800] + ('...' if len(desc) > 800 else ''))

        result = "\n".join(md)
        self._progress('linkedin', f"LinkedIn: got {result.count(chr(10))} lines for {org.get('name') or 'unknown'}")
        self.log.append({'tool': 'linkedin', 'input': linkedin_url, 'output': result[:300]})
        return result

    # ── Find a company's LinkedIn URL via Google (Web Unlocker) ────────────────
    def find_linkedin_url(self, query: str):
        """Google '<query> linkedin company' through the Bright Data Web Unlocker and return
        the first linkedin.com/company/<slug> URL (or None). Self-contained + synchronous —
        avoids the SERP dataset API, which is now async (202 + snapshot polling)."""
        from urllib.parse import quote_plus
        api_key = self.config.get('brightdata_api_key') or ''
        if not api_key:
            return None
        self._progress('google', f"Google: searching LinkedIn for {query}...")
        self.count('brightdata'); self.count('linkedin', op='find')
        search_url = ('https://www.google.com/search?q='
                      + quote_plus(f"{query} linkedin company") + '&num=20')
        payload = {
            'zone': self.config.get('brightdata_zone') or 'web_unlocker1',
            'url': search_url,
            'format': 'raw',
        }
        try:
            r = requests.post(
                'https://api.brightdata.com/request',
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
                data=json.dumps(payload), timeout=60, allow_redirects=False,
            )
            html = r.text if r.status_code == 200 else None
        except requests.RequestException:
            html = None
        if not html:
            self._progress('google', "Google: search failed")
            return None
        m = re.search(r'linkedin\.com/company/[A-Za-z0-9_\-\.%]+', html, re.I)
        if not m:
            self._progress('google', "Google: no LinkedIn company link found")
            return None
        # strip any trailing junk, normalise to a clean https URL
        slug = m.group(0)
        url = 'https://www.' + slug.rstrip('.')
        self._progress('google', f"Found LinkedIn: {url}")
        return url

    # ── LinkedIn company page (structured) ────────────────────────────────────
    def linkedin_company_data(self, url: str):
        """Fetch a LinkedIn company page (Bright Data Web Unlocker) and return the parsed
        Organization as a structured dict (or None). `employees` = LD+JSON
        numberOfEmployees.value — the headline figure the LinkedIn Finder tool is after."""
        api_key = self.config.get('brightdata_api_key') or ''
        if not api_key:
            return None
        self._progress('linkedin', f"Fetching LinkedIn: {url}...")
        self.count('brightdata'); self.count('linkedin', op='data')
        payload = {
            'zone': self.config.get('brightdata_zone') or 'web_unlocker1',
            'url': url,
            'format': 'raw',
        }
        try:
            r = requests.post(
                'https://api.brightdata.com/request',
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
                data=json.dumps(payload), timeout=60, allow_redirects=False,
            )
            html = r.text if r.status_code == 200 else None
        except requests.RequestException:
            html = None
        if not html:
            self._progress('linkedin', "LinkedIn: fetch failed")
            return None

        org = _extract_ld_org(html)
        if not org:
            self._progress('linkedin', "LinkedIn: no Organization data found")
            return None

        emp = org.get('numberOfEmployees')
        employees = emp.get('value') if isinstance(emp, dict) else emp
        try:
            employees = int(employees) if employees not in (None, '') else None
        except (ValueError, TypeError):
            employees = None

        addr = org.get('address') if isinstance(org.get('address'), dict) else {}
        address = ', '.join([p for p in [
            addr.get('streetAddress'), addr.get('addressLocality'),
            addr.get('postalCode'), addr.get('addressCountry'),
        ] if p])

        self._progress('linkedin', f"LinkedIn: {org.get('name') or 'unknown'} — {employees} employees")
        return {
            'linkedin_url': url,
            'name': org.get('name'),
            'employees': employees,
            'website': org.get('sameAs'),
            'slogan': org.get('slogan'),
            'description': org.get('description'),
            'address': address or None,
            'address_locality': addr.get('addressLocality'),
            'address_country': addr.get('addressCountry'),
            'org': org,
        }

    # ── Yahoo Finance ─────────────────────────────────────────────────────────
    def yahoo_finance_data(self, ticker: str) -> str:
        self._progress('yahoo', f"Yahoo Finance: fetching data for {ticker}...")
        self.count('yahoo')

        # Step 1: Get crumb + cookies (shared cookie jar via a Session)
        session = requests.Session()
        try:
            session.get('https://fc.yahoo.com/t',
                        headers={'User-Agent': 'Mozilla/5.0'},
                        timeout=15, allow_redirects=False)
        except requests.RequestException:
            pass

        try:
            r = session.get('https://query2.finance.yahoo.com/v1/test/getcrumb',
                            headers={'User-Agent': 'Mozilla/5.0'},
                            timeout=15, allow_redirects=False)
            crumb = r.text
        except requests.RequestException:
            crumb = None

        if not crumb or len(crumb) > 50:
            self._progress('yahoo', "Yahoo Finance: failed to get crumb")
            return ''

        # Step 2: Fetch profile + financials
        modules = 'assetProfile,incomeStatementHistory,balanceSheetHistory'
        url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
               f"?modules={modules}&crumb=" + quote_plus(crumb))
        try:
            r = session.get(url, headers={'User-Agent': 'Mozilla/5.0'},
                            timeout=20, allow_redirects=False)
            resp = r.text
        except requests.RequestException:
            resp = None

        try:
            data = json.loads(resp) if resp else None
        except (ValueError, TypeError):
            data = None
        results = ((data or {}).get('quoteSummary') or {}).get('result') or []
        if not results:
            self._progress('yahoo', f"Yahoo Finance: no data for {ticker}")
            self.log.append({'tool': 'yahoo_finance_data', 'input': ticker, 'output': 'No data'})
            return ''

        res0 = results[0]
        md = []
        md.append(f"### Yahoo Finance — {ticker}")
        md.append(f"Source: https://finance.yahoo.com/quote/{ticker}/")
        md.append('')

        # Profile
        profile = res0.get('assetProfile') or {}
        if profile:
            md.append('**Company Profile**')
            fields = {
                'website': 'Website',
                'sector': 'Sector',
                'industry': 'Industry',
                'fullTimeEmployees': 'Employees',
                'city': 'City',
                'state': 'State',
                'country': 'Country',
            }
            for key, label in fields.items():
                val = profile.get(key)
                if val is not None and val != '':
                    if key == 'fullTimeEmployees':
                        val = _php_number_format(val)
                    md.append(f"- {label}: {val}")

            # Officers
            officers = profile.get('companyOfficers') or []
            if officers:
                md.append('')
                md.append('**Key Officers**')
                for officer in officers[:5]:
                    name = officer.get('name') or 'Unknown'
                    title = officer.get('title') or ''
                    md.append(f"- {name}" + (f" — {title}" if title else ''))

            # Business summary
            summary = profile.get('longBusinessSummary') or ''
            if summary:
                md.append('')
                md.append('**Description**')
                md.append(summary[:500] + ('...' if len(summary) > 500 else ''))
            md.append('')

        # Income Statement
        income_stmts = ((res0.get('incomeStatementHistory') or {}).get('incomeStatementHistory')) or []
        if income_stmts:
            md.append('**Income Statement (Annual)**')
            md.append('| Period | Revenue | Net Income |')
            md.append('|---|---|---|')
            for stmt in income_stmts[:3]:
                date_v = (stmt.get('endDate') or {}).get('fmt') or '?'
                rev = self._yahoo_format_val(stmt.get('totalRevenue') or {})
                ni = self._yahoo_format_val(stmt.get('netIncome') or {})
                md.append(f"| {date_v} | {rev} | {ni} |")
            md.append('')

        # Balance Sheet
        balance_stmts = ((res0.get('balanceSheetHistory') or {}).get('balanceSheetStatements')) or []
        if balance_stmts:
            md.append('**Balance Sheet (Most Recent)**')
            bs = balance_stmts[0]
            date_v = (bs.get('endDate') or {}).get('fmt') or '?'
            md.append(f"As of {date_v}:")
            bs_fields = {
                'totalAssets': 'Total Assets',
                'totalLiab': 'Total Liabilities',
                'totalStockholderEquity': 'Stockholder Equity',
                'cash': 'Cash',
            }
            for key, label in bs_fields.items():
                val = self._yahoo_format_val(bs.get(key) or {})
                if val != '—':
                    md.append(f"- {label}: {val}")

        result = "\n".join(md)
        line_count = result.count("\n")
        self._progress('yahoo', f"Yahoo Finance: got {line_count} lines for {ticker}")
        self.log.append({'tool': 'yahoo_finance_data', 'input': ticker, 'output': result[:500]})
        return result

    def _yahoo_format_val(self, field: dict) -> str:
        raw = field.get('raw') if isinstance(field, dict) else None
        if raw is None:
            return '—'
        abs_v = abs(raw)
        sign = '-' if raw < 0 else ''
        if abs_v >= 1e12:
            return sign + _php_number_format(abs_v / 1e12, 1) + 'T'
        if abs_v >= 1e9:
            return sign + _php_number_format(abs_v / 1e9, 1) + 'B'
        if abs_v >= 1e6:
            return sign + _php_number_format(abs_v / 1e6, 1) + 'M'
        if abs_v >= 1e3:
            return sign + _php_number_format(abs_v / 1e3, 1) + 'K'
        return sign + _php_number_format(abs_v)
