"""
Entity Lookup v3b (Python) — SEC EDGAR / IAPD tool cluster.

Faithful, like-for-like port of the SEC methods from php/tools.php (class LookupTools):
searchSecCompany, searchSecFulltext, fetchSecSubmissions, fetchSec8K, fetchSecFiling,
secEdgarFinancials, edgarParentSearch, searchSecIapd.

This is a mixin (`SecMixin`) that is combined with `ToolBase` (toolbase.py) via multiple
inheritance in tools.py. It CALLS the shared helpers on `self` rather than reimplementing
them:
    self.config                      — incl. 'sec_user_agent', 'max_ciks'
    self._progress(phase, message)   — progress callback (exact strings preserved)
    self.html_to_text(html)          — htmlToText port
    self.http_get(url, headers=None, timeout=20) -> str | None  — httpGet port

SEC EDGAR requires a User-Agent header = self.config['sec_user_agent']; the PHP passed it
as the 2nd arg of httpGet(), here it is applied as headers={'User-Agent': ...}. The httpGet
port uses a 15s curl timeout, so http_get is called with timeout=15 to match.

Stdlib + requests only.
"""
from __future__ import annotations

import html as _html
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote_plus, urlencode

import requests


class SecMixin:
    # ── log helper (PHP appended to $this->log[]) ───────────────────────────
    def _log(self, tool: str, input, output) -> None:
        if not hasattr(self, 'log') or self.log is None:
            self.log = []
        self.log.append({'tool': tool, 'input': input, 'output': output})

    # ── small helpers mirroring PHP null-coalesce / number_format / json ────
    @staticmethod
    def _nn(v, default):
        """PHP `$v ?? $default` — default only on null/unset."""
        return v if v is not None else default

    @staticmethod
    def _dig(obj, *keys, default=None):
        """PHP nested `$a['x']['y'] ?? $default`."""
        cur = obj
        for k in keys:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                return default
        return cur if cur is not None else default

    @staticmethod
    def _number_format(num, decimals: int = 0) -> str:
        """PHP number_format(): thousands-separated, round-half-away-from-zero."""
        q = Decimal(str(num)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
        return f"{q:,.{decimals}f}"

    @staticmethod
    def _php_json_encode(data, pretty: bool = False) -> str:
        """Mirror PHP json_encode() default behaviour: escape '/' as '\\/',
        \\uXXXX for non-ASCII. JSON_PRETTY_PRINT => 4-space indent."""
        if pretty:
            s = json.dumps(data, indent=4, ensure_ascii=True)
        else:
            s = json.dumps(data, separators=(',', ':'), ensure_ascii=True)
        # PHP escapes forward slashes by default; only string-value slashes exist here.
        return s.replace('/', '\\/')

    # ── searchSecCompany (tools.php:1042) ───────────────────────────────────
    def search_sec_company(self, query: str) -> str:
        # SEC EDGAR stores names without periods or commas (e.g. "AMAZON COM INC" not "Amazon.com, Inc.")
        clean_query = query.replace('.', ' ').replace(',', ' ')
        clean_query = re.sub(r'\s+', ' ', clean_query.strip())
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?company=" + quote_plus(clean_query)
               + "&CIK=&type=&dateb=&owner=include&count=20&search_text=&action=getcompany")
        html = self.http_get(url, headers={'User-Agent': self.config['sec_user_agent']}, timeout=15)
        if not html:
            return "Error: Could not reach SEC EDGAR."

        results = []
        # Multi-result: table rows with CIK + name
        matches = re.findall(
            r'<tr[^>]*>.*?<td[^>]*><a[^>]*>([^<]+)</a></td>\s*<td[^>]*>([^<]*)</td>',
            html, re.I | re.S)
        if matches:
            for match in matches:
                cik = match[0].strip()
                name = _html.unescape(match[1]).strip()
                if cik and name:
                    results.append(f"CIK: {cik} | {name}")
        # Single-result: SEC redirects to company detail page with companyName span
        if not results:
            m = re.search(r'<span class="companyName">(.+?)<acronym.*?CIK.*?(\d{10})',
                          html, re.I | re.S)
            if m:
                name = _html.unescape(m.group(1)).strip()
                cik = m.group(2).strip()
                if name and cik:
                    results.append(f"CIK: {cik} | {name}")

        result = "\n".join(results[:20]) if results else "No SEC company results found."
        self._log('search_sec_company', query, result)
        return result

    # ── searchSecFulltext (tools.php:1079) ──────────────────────────────────
    def search_sec_fulltext(self, query: str) -> str:
        url = "https://efts.sec.gov/LATEST/search-index?q=" + quote_plus(query)
        raw = self.http_get(url, headers={'User-Agent': self.config['sec_user_agent']}, timeout=15)
        if not raw:
            return "Error: Could not reach SEC fulltext."

        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
        if not data:
            return "Error: Invalid SEC fulltext response."

        total = self._dig(data, 'hits', 'total', 'value', default=0)
        hits = self._dig(data, 'hits', 'hits', default=[]) or []
        lines = [f"Total hits: {total}"]
        for h in hits[:10]:
            s = (h.get('_source') if isinstance(h, dict) else None) or {}
            names = ', '.join(s.get('display_names') or [])
            lines.append("  " + self._nn(s.get('file_date'), '') + f" | {names} | "
                         + self._nn(s.get('form_type'), ''))

        result = "\n".join(lines)
        self._log('search_sec_fulltext', query, result)
        return result

    # ── fetchSecSubmissions (tools.php:1106) ────────────────────────────────
    def fetch_sec_submissions(self, cik: str) -> str:
        cik_padded = str(cik).rjust(10, '0')
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        raw = self.http_get(url, headers={'User-Agent': self.config['sec_user_agent']}, timeout=15)
        if not raw:
            return "Error: Could not fetch SEC submissions."

        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
        if not data:
            return "Error: Invalid SEC submissions response."

        info = {
            'name': data.get('name'),
            'cik': data.get('cik'),
            'tickers': data.get('tickers') if data.get('tickers') is not None else [],
            'exchanges': data.get('exchanges') if data.get('exchanges') is not None else [],
            'sic': data.get('sic'),
            'sicDescription': data.get('sicDescription'),
            'entityType': data.get('entityType'),
            'formerNames': data.get('formerNames') if data.get('formerNames') is not None else [],
            'addresses': data.get('addresses') if data.get('addresses') is not None else [],
            'phone': data.get('phone'),
        }

        recent = self._dig(data, 'filings', 'recent', default={}) or {}
        forms = recent.get('form') if isinstance(recent, dict) else None
        forms = forms or []
        info['total_filings'] = len(forms)

        if forms:
            info['latest_filings'] = []
            filing_dates = recent.get('filingDate') or []
            accessions = recent.get('accessionNumber') or []
            primary_docs = recent.get('primaryDocument') or []
            for i in range(min(20, len(forms))):
                form_type = forms[i]
                # Keep first 5 of any type, plus any 8-K in the first 20
                if len(info['latest_filings']) < 5 or form_type == '8-K':
                    info['latest_filings'].append({
                        'form': form_type,
                        'date': filing_dates[i] if i < len(filing_dates) else '',
                        'accession': accessions[i] if i < len(accessions) else '',
                        'primaryDocument': primary_docs[i] if i < len(primary_docs) else '',
                    })
                # Stop once we have the basics + at least one 8-K
                if len(info['latest_filings']) >= 5:
                    has_8k = False
                    for f in info['latest_filings']:
                        if f['form'] == '8-K':
                            has_8k = True
                            break
                    if has_8k:
                        break

        result = self._php_json_encode(info, pretty=True)
        self._log('fetch_sec_submissions', cik, result)
        return result

    # ── fetchSec8K (tools.php:1170) — exposed as fetch_sec ───────────────────
    def fetch_sec(self, cik: str, submissions: dict):
        """Fetch and parse the cover page of the most recent 8-K filing for a CIK.
        Returns structured entity data (dict) or None."""
        # Find the most recent 8-K in submissions
        recent = (submissions.get('latest_filings') if isinstance(submissions, dict) else None) or []
        filing = None
        for f in recent:
            if (f.get('form') or '') == '8-K' and f.get('primaryDocument'):
                filing = f
                break
        if not filing:
            return None

        accession = filing['accession'].replace('-', '')
        cik_clean = str(cik).lstrip('0')
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{accession}/"
               f"{filing['primaryDocument']}")

        html = self.http_get(url, headers={'User-Agent': self.config['sec_user_agent']}, timeout=15)
        if not html:
            return None

        # Strip HTML tags, decode entities, split into lines
        text = re.sub(r'<[^>]+>', "\n", html)
        text = _html.unescape(text)
        # Filter out empty lines and lines that are just whitespace/nbsp
        lines = []
        for l in text.split("\n"):
            l = l.strip()
            clean = re.sub(r'[\s ]+', '', l)
            if clean != '':
                lines.append(l)

        result = {'filing_url': url, 'filing_date': filing.get('date') or ''}

        # Find key lines by their label text, then grab the line(s) before them
        for i, line in enumerate(lines):
            if 'exact name of registrant' in line.lower() and i > 0:
                result['registered_name'] = lines[i - 1].strip()
            if 'state or other jurisdiction' in line.lower():
                # State, file number, EIN are on 3 separate lines between "(Exact name..." and this label
                vals = []
                for j in range(i - 1, max(0, i - 6) - 1, -1):
                    v = lines[j].strip()
                    if v.startswith('('):
                        continue
                    if v and len(v) < 50:
                        vals.append(v)
                    if len(vals) >= 3:
                        break
                vals = list(reversed(vals))
                if len(vals) >= 3:
                    result['state_of_incorporation'] = vals[0]
                    result['commission_file_number'] = vals[1]
                    result['irs_ein'] = vals[2]
            if 'address of principal executive offices' in line.lower() and i > 0:
                # Address spans multiple lines above — collect until we hit a label or known field
                addr = []
                for j in range(i - 1, max(0, i - 6) - 1, -1):
                    v = lines[j].strip()
                    if not v or v.startswith('(') or 'identification' in v.lower():
                        break
                    # Skip bare punctuation
                    if v == ',' or v == '.':
                        continue
                    # Skip bare state abbreviations that will be combined
                    addr.insert(0, v)
                if addr:
                    # Join and clean up
                    result['address'] = re.sub(r'\s+', ' ', ' '.join(addr))
            if 'telephone' in line.lower() and 'registrant' in line.lower() and i > 0:
                # Phone digits may be split across lines: "(" "650" ")" "253-0000"
                phone_parts = []
                for j in range(i - 1, max(0, i - 5) - 1, -1):
                    v = lines[j].strip()
                    if v.startswith('(') and 'address' in v.lower():
                        break
                    if re.search(r'[\d()\-]', v):
                        phone_parts.insert(0, v)
                    else:
                        break
                if phone_parts:
                    phone = ''.join(phone_parts)
                    phone = re.sub(r'[^\d()\-\s]', '', phone)
                    result['phone'] = phone.strip()
            if 'former name or former address' in line.lower() and i > 0:
                former = lines[i - 1].strip()
                if former and not re.match(r'(?i)^(Not Applicable|No Change|N/A|None)$', former):
                    result['former_name'] = former

        self._log('fetch_sec_8k', cik, self._php_json_encode(result))
        return result

    # ── fetchSecFiling (tools.php:1271) ─────────────────────────────────────
    def fetch_sec_filing(self, url: str) -> str:
        content = self.http_get(url, headers={'User-Agent': self.config['sec_user_agent']}, timeout=15)
        if not content:
            return "Error: Could not fetch SEC filing."

        if '<html' in content.lower():
            content = self.html_to_text(content)

        if len(content) > 10000:
            content = content[:10000] + "\n... [truncated]"

        self._log('fetch_sec_filing', url, content)
        return content

    # ── secEdgarFinancials (tools.php:1290) ─────────────────────────────────
    def sec_edgar_financials(self, cik: str) -> str:
        padded_cik = str(cik).rjust(10, '0')
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{padded_cik}.json"
        raw = self.http_get(url, headers={'User-Agent': self.config['sec_user_agent']}, timeout=15)
        if not raw:
            self._log('sec_edgar_financials', cik, 'No data')
            return ''

        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
        if not data or not data.get('facts'):
            self._log('sec_edgar_financials', cik, 'Invalid JSON')
            return ''

        entity_name = self._nn(data.get('entityName'), 'Unknown')
        gaap = self._dig(data, 'facts', 'us-gaap', default={}) or {}
        dei = self._dig(data, 'facts', 'dei', default={}) or {}
        all_facts = {**gaap, **dei}

        targets = {
            'Revenue': ['RevenueFromContractWithCustomerExcludingAssessedTax',
                        'RevenueFromContractWithCustomerIncludingAssessedTax', 'Revenues',
                        'RevenuesNetOfInterestExpense', 'SalesRevenueNet'],
            'Net Income': ['NetIncomeLoss'],
            'Total Assets': ['Assets'],
            'Total Equity': ['StockholdersEquity'],
            'Operating Income': ['OperatingIncomeLoss'],
            'Cash': ['CashAndCashEquivalentsAtCarryingValue'],
            'Total Liabilities': ['Liabilities'],
        }

        year_data = {}  # metric => year => value
        all_years = {}

        for label, tags in targets.items():
            for tag in tags:
                if tag not in all_facts:
                    continue
                units = (all_facts[tag].get('units') if isinstance(all_facts[tag], dict) else None) or {}
                for unit_name, entries in units.items():
                    annual = [e for e in entries
                              if (e.get('form') or '') == '10-K' and (e.get('fp') or '') == 'FY']
                    for entry in annual:
                        year = (entry.get('end') or '')[0:4]
                        if not year:
                            continue
                        all_years[year] = True
                        year_data.setdefault(label, {})
                        if year not in year_data[label]:
                            year_data[label][year] = entry['val']
                if year_data.get(label):
                    break  # use first matching tag

        if not year_data or not all_years:
            self._log('sec_edgar_financials', cik, 'No annual data')
            return ''

        # Most recent 3 years
        years = list(all_years.keys())
        years.sort()
        years = years[-3:]

        md = []
        md.append(f"### SEC EDGAR Financials — {entity_name}")
        md.append('')
        header = '| Metric | ' + ' | '.join(years) + ' |'
        sep = '|---|' + '|'.join(['---'] * len(years)) + '|'
        md.append(header)
        md.append(sep)

        for label, tags in targets.items():
            if label not in year_data:
                continue
            vals = []
            for y in years:
                if y in year_data[label]:
                    v = year_data[label][y]
                    if abs(v) >= 1e9:
                        vals.append('$' + self._number_format(v / 1e9, 1) + 'B')
                    elif abs(v) >= 1e6:
                        vals.append('$' + self._number_format(v / 1e6, 1) + 'M')
                    else:
                        vals.append('$' + self._number_format(v))
                else:
                    vals.append('—')
            md.append('| ' + label + ' | ' + ' | '.join(vals) + ' |')

        result = "\n".join(md)
        self._log('sec_edgar_financials', cik, result[:500])
        return result

    # ── edgarParentSearch (tools.php:3058) ──────────────────────────────────
    def edgar_parent_search(self, subsidiary_name: str) -> str:
        ua = self.config['sec_user_agent']
        lines = []

        # Step 1: Search EFTS for the subsidiary name in 10-K filings
        # Generate name variants (with/without comma before legal suffix)
        name_variants = [subsidiary_name]
        m = re.match(r'(?i)^(.+),\s*(LLC|Inc|Ltd|Corp|LP|LLP)\.?$', subsidiary_name)
        if m:
            name_variants.append(m.group(1) + ' ' + m.group(2))
        else:
            m = re.match(r'(?i)^(.+)\s+(LLC|Inc|Ltd|Corp|LP|LLP)\.?$', subsidiary_name)
            if m:
                name_variants.append(m.group(1) + ', ' + m.group(2))
        name_variants = list(dict.fromkeys(name_variants))

        # Try with "subsidiaries of the registrant" first (strongest signal)
        searches = []
        for name in name_variants:
            searches.append('"' + name + '" "subsidiaries of the registrant"')
        for name in name_variants:
            searches.append('"' + name + '" "Exhibit 21"')
        for name in name_variants:
            searches.append('"' + name + '" "significant subsidiaries"')
        for name in name_variants:
            searches.append('"' + name + '"')

        exhibit21_hits = {}
        other_hits = {}

        for query in searches:
            url = ("https://efts.sec.gov/LATEST/search-index?q=" + quote_plus(query)
                   + "&forms=10-K&dateRange=custom&startdt=2022-01-01&enddt=2026-12-31")
            raw = self.http_get(url, headers={'User-Agent': ua}, timeout=15)
            if not raw:
                continue

            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                data = None
            if not data:
                continue

            for h in (self._dig(data, 'hits', 'hits', default=[]) or []):
                s = (h.get('_source') if isinstance(h, dict) else None) or {}
                cik = (s.get('ciks') or [''])[0] if (s.get('ciks')) else ''
                file_type = s.get('file_type') or ''
                file_desc = s.get('file_description') or ''
                display_name = (s.get('display_names') or [''])[0] if (s.get('display_names')) else ''
                file_date = s.get('file_date') or ''
                adsh = s.get('adsh') or ''

                # Deduplicate by CIK+date
                key = cik + '|' + file_date

                is_exhibit21 = ('21' in file_type.lower()
                                or 'subsidiar' in file_desc.lower()
                                or '21' in file_desc.lower())

                if is_exhibit21 and key not in exhibit21_hits:
                    exhibit21_hits[key] = {
                        'cik': cik,
                        'name': display_name,
                        'date': file_date,
                        'adsh': adsh,
                        'file_type': file_type,
                    }
                elif not is_exhibit21 and key not in other_hits:
                    other_hits[key] = {
                        'cik': cik,
                        'name': display_name,
                        'date': file_date,
                        'file_type': file_type,
                    }
            # Stop searching if we found Exhibit 21 hits
            if exhibit21_hits:
                break

        if not exhibit21_hits and not other_hits:
            result = (f"No EDGAR parent found for \"{subsidiary_name}\". "
                      f"Entity may not be a subsidiary of a US public company.")
            self._log('edgar_parent_search', subsidiary_name, result)
            return result

        # Step 2: For Exhibit 21 hits, fetch the actual exhibit to confirm
        # Sort by date descending (most recent first)
        exhibit21_sorted = sorted(exhibit21_hits.values(), key=lambda h: h['date'], reverse=True)

        for hit in exhibit21_sorted[:3]:
            cik_clean = hit['cik'].lstrip('0')
            adsh_clean = hit['adsh'].replace('-', '')

            # Fetch filing index to find Exhibit 21 document URL
            index_url = (f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{adsh_clean}/"
                         f"{hit['adsh']}-index.htm")
            index_html = self.http_get(index_url, headers={'User-Agent': ua}, timeout=15)

            exhibit_url = None
            if index_html:
                mm = re.search(r'href="(/Archives[^"]*ex[^"]*21[^"]*?)"', index_html, re.I)
                if mm:
                    exhibit_url = "https://www.sec.gov" + mm.group(1)

            if exhibit_url:
                # Fetch exhibit content and check for subsidiary name
                exhibit_html = self.http_get(exhibit_url, headers={'User-Agent': ua}, timeout=15)
                if exhibit_html:
                    exhibit_text = self.html_to_text(exhibit_html)
                    name_in_exhibit = False
                    for variant in name_variants:
                        if variant.lower() in exhibit_text.lower():
                            name_in_exhibit = True
                            break

                    # Extract parent name from display_names (strip ticker/CIK suffix)
                    parent_name = re.sub(r'\s*\(.*$', '', hit['name'])
                    parent_name = parent_name.strip()

                    lines.append("EDGAR PARENT FOUND (Exhibit 21):")
                    lines.append(f"  Parent: {parent_name}")
                    lines.append(f"  Parent CIK: {hit['cik']}")
                    lines.append(f"  Filing date: {hit['date']}")
                    lines.append(f"  Exhibit URL: {exhibit_url}")

                    if name_in_exhibit:
                        lines.append(f"  Confirmation: \"{subsidiary_name}\" appears in Exhibit 21 "
                                     f"— STRONG evidence of subsidiary relationship.")
                    else:
                        lines.append("  Confirmation: Name not found verbatim in exhibit text. "
                                     "May use a variant name.")

                    # Extract other subsidiaries listed (for context)
                    exhibit_lines = exhibit_text.split("\n")
                    subsidiary_list = []
                    in_list = False
                    for el in exhibit_lines:
                        el = el.strip()
                        if ('subsidiaries of the registrant' in el.lower()
                                or 'name of subsidiary' in el.lower()):
                            in_list = True
                            continue
                        if (in_list and len(el) > 3 and len(el) < 120
                                and not re.match(r'(?i)^(jurisdiction|name of|exhibit|document|ex-)', el)):
                            subsidiary_list.append(el)
                    if subsidiary_list:
                        lines.append("  Other subsidiaries listed: "
                                     + ', '.join(subsidiary_list[:10]))

                    break  # We found and confirmed, stop

            # If we couldn't fetch the exhibit, still report the finding
            if not lines:
                parent_name = re.sub(r'\s*\(.*$', '', hit['name'])
                lines.append("EDGAR PARENT FOUND (Exhibit 21 reference):")
                lines.append("  Parent: " + parent_name.strip())
                lines.append(f"  Parent CIK: {hit['cik']}")
                lines.append(f"  Filing date: {hit['date']}")
                lines.append("  Note: Could not fetch exhibit to confirm. MEDIUM evidence.")
                break

        # If no Exhibit 21 hits but other mentions exist
        if not lines and other_hits:
            hit = list(other_hits.values())[0]
            parent_name = re.sub(r'\s*\(.*$', '', hit['name'])
            lines.append("EDGAR MENTION ONLY (no Exhibit 21):")
            lines.append("  Mentioned in filings of: " + parent_name.strip())
            lines.append(f"  CIK: {hit['cik']}")
            lines.append(f"  Filing date: {hit['date']}")
            lines.append(f"  File type: {hit['file_type']}")
            lines.append("  Note: Mentioned in filing but not confirmed as subsidiary. "
                         "WEAK evidence — could be customer, supplier, counterparty, etc.")

        result = "\n".join(lines)
        self._log('edgar_parent_search', subsidiary_name, result)
        return result

    # ── searchSecIapd (tools.php:3252) ──────────────────────────────────────
    def search_sec_iapd(self, firm_name: str) -> str:
        self._progress('registry', f"Searching SEC IAPD for \"{firm_name}\"...")

        params = {
            'query': firm_name,
            'offset': 0,
            'count': 10,
        }
        url = 'https://api.adviserinfo.sec.gov/search/firm?' + urlencode(params)

        # Direct request: PHP used a bespoke curl (Accept: application/json, no User-Agent,
        # CURLOPT_ENCODING => '' to accept all encodings, 15s timeout) and needs the HTTP
        # status code for the error strings, so this does not route through http_get().
        try:
            resp = requests.get(url, headers={'Accept': 'application/json'}, timeout=15,
                                allow_redirects=True)
            http_code = resp.status_code
            response = resp.text
        except requests.RequestException:
            http_code = 0
            response = ''

        if http_code != 200 or not response:
            result = f"No SEC IAPD results (HTTP {http_code})."
            self._log('sec_iapd', firm_name, result)
            return result

        try:
            data = json.loads(response)
        except (ValueError, TypeError):
            data = None

        hits = self._dig(data, 'hits', 'hits', default=[]) or []
        total = self._dig(data, 'hits', 'total', default=0)

        if not hits:
            result = f"No SEC IAPD results for \"{firm_name}\"."
            self._log('sec_iapd', firm_name, result)
            return result

        self._progress('registry', f"SEC IAPD: {total} results for \"{firm_name}\"")

        lines = [f"SEC IAPD: {total} result(s) for \"{firm_name}\"", ""]
        for hit in hits:
            src = (hit.get('_source') if isinstance(hit, dict) else None) or {}
            name = self._nn(src.get('firm_name'), 'Unknown')
            sec_num = self._nn(src.get('firm_ia_full_sec_number'), 'N/A')
            scope = self._nn(src.get('firm_ia_scope'), 'Unknown')
            other_names = src.get('firm_other_names') if src.get('firm_other_names') is not None else []
            branches = self._nn(src.get('firm_branches_count'), 0)
            has_disclosures = self._nn(src.get('firm_ia_disclosure_fl'), 'N') == 'Y'

            lines.append(f"• {name}")
            lines.append(f"  SEC#: {sec_num} | Status: {scope} | Branches: {branches}")
            if has_disclosures:
                lines.append("  ⚠ Has regulatory disclosures")

            # Parse address
            addr_json = src.get('firm_ia_address_details') or ''
            if addr_json:
                try:
                    addr_data = json.loads(addr_json)
                except (ValueError, TypeError):
                    addr_data = None
                office = (addr_data.get('officeAddress') if isinstance(addr_data, dict) else None) or {}
                raw_parts = [
                    office.get('street1') or '', office.get('street2') or '',
                    office.get('city') or '', office.get('state') or '',
                    office.get('postalCode') or '', office.get('country') or '',
                ]
                # PHP array_filter drops falsy values (incl. '' and '0')
                addr_parts = [p for p in raw_parts if p and p != '0']
                if addr_parts:
                    lines.append("  Address: " + ', '.join(addr_parts))

            # Other names (skip if same as firm_name)
            other_filtered = [n for n in other_names if n.lower() != name.lower()]
            if other_filtered:
                lines.append("  Also known as: " + '; '.join(other_filtered))

            sid = src.get('firm_source_id')
            sid = '' if sid is None else sid
            lines.append("  IAPD URL: https://adviserinfo.sec.gov/firm/summary/" + str(sid))
            lines.append("")

        result = "\n".join(lines)
        self._log('sec_iapd', firm_name, f"{len(hits)} results")
        return result
