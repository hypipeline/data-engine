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

import json
import math
import re
from urllib.parse import quote_plus

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
        api_key = self.config.get('brightdata_api_key') or ''
        result = {'google_results': '', 'yahoo_ticker': None, 'linkedin_url': None}

        if not api_key:
            self._progress('google', "Google Intelligence: Bright Data not configured")
            return result

        self._progress('google', f"Google Intelligence: searching 3 queries for {domain}...")
        self.api_calls['brightdata'] += 1

        payload = {
            'input': [
                {'url': 'https://www.google.com/', 'keyword': domain},
                {'url': 'https://www.google.com/', 'keyword': f"site:finance.yahoo.com {domain}"},
                {'url': 'https://www.google.com/', 'keyword': f"{domain} linkedin"},
            ],
            'limit_per_input': 10,
        }
        try:
            r = requests.post(
                'https://api.brightdata.com/datasets/v3/scrape'
                '?dataset_id=gd_mfz5x93lmsjjjylob&notify=false&include_errors=true',
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}',
                },
                data=json.dumps(payload),
                timeout=90,
                allow_redirects=False,
            )
            http_code = r.status_code
            resp = r.text
        except requests.RequestException:
            http_code = 0
            resp = None

        if http_code != 200 or not resp:
            self._progress('google', f"Google Intelligence: SERP failed (HTTP {http_code})")
            self.log.append({'tool': 'google_intelligence', 'input': domain, 'output': f"HTTP {http_code}"})
            return result

        # Response is NDJSON (one JSON object per line)
        lines = resp.strip().split("\n")
        serp_results = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except (ValueError, TypeError):
                parsed = None
            if parsed:
                serp_results.append(parsed)

        self._progress('google', f"Google Intelligence: got {len(serp_results)} SERP results")

        # Process each result by keyword
        google_md = []
        for serp in serp_results:
            keyword = serp.get('keyword') or ''
            organic = serp.get('organic') or []

            if keyword == domain:
                # General Google results — format top results as markdown
                google_md.append(f"### Google Search Results for {domain}")
                google_md.append('')
                for rr in organic[:10]:
                    title = rr.get('title') or ''
                    link = rr.get('link') or ''
                    desc = rr.get('description') or ''
                    google_md.append(f"- **{title}**")
                    google_md.append(f"  {link}")
                    if desc:
                        google_md.append(f"  {desc}")
            elif keyword.startswith('site:finance.yahoo.com'):
                # Yahoo Finance ticker extraction
                for rr in organic:
                    link = rr.get('link') or ''
                    m = re.search(r'finance\.yahoo\.com/quote/([A-Z0-9a-z.\-]+)', link)
                    if m:
                        result['yahoo_ticker'] = m.group(1)
                        self._progress('google', f"Found Yahoo Finance ticker: {m.group(1)}")
                        break
            elif 'linkedin' in keyword:
                # LinkedIn URL extraction — find company page
                for rr in organic:
                    link = rr.get('link') or ''
                    if re.search(r'linkedin\.com/company/[a-z0-9\-]+', link, re.I):
                        result['linkedin_url'] = link
                        self._progress('google', f"Found LinkedIn: {link}")
                        break

        result['google_results'] = "\n".join(google_md)
        self.log.append({
            'tool': 'google_intelligence',
            'input': domain,
            'output':
                "google:" + str(len(result['google_results'])) + " chars, " +
                "yahoo:" + (result['yahoo_ticker'] or 'none') + ", " +
                "linkedin:" + (result['linkedin_url'] or 'none'),
        })
        return result

    # ── LinkedIn company page (Bright Data Web Unlocker, raw html) ─────────────
    def fetch_linkedin_company(self, linkedin_url: str) -> str:
        api_key = self.config.get('brightdata_api_key') or ''
        if not api_key:
            return ''

        self._progress('linkedin', f"Fetching LinkedIn: {linkedin_url}...")
        self.api_calls['brightdata'] += 1

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
        self.api_calls['brightdata'] += 1
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
        self.api_calls['brightdata'] += 1
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
