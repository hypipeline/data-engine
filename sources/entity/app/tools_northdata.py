"""
Entity Lookup v3b (Python) — North Data tool cluster.

Faithful, like-for-like port of the North Data methods of php/tools.php
(class LookupTools): searchNorthdata, validateNorthdataEntity, northdataNetwork,
plus every private helper they depend on (login/auth-cookie handling, northdataGet,
HTML/JSON parsing, network/financials/publications extraction).

This is a mixin: `class NorthDataMixin` is combined with ToolBase (and the other
tool clusters) via multiple inheritance in tools.py. It relies on the following
attributes/helpers provided by ToolBase (do NOT reimplement them here):
    - self.config                       (incl. 'northdata_email', 'northdata_password',
                                          'browserbase_api_key', 'browserbase_project_id')
    - self.northdata_auth_cookie        (instance attr, starts None — the session cookie)
    - self._progress(phase, message)    (exact progress strings)
    - self.html_to_text(html)           (htmlToText port)
    - self.browserbase_fetch_html(url)  (browserbaseFetchHtml port; used by _northdata_search_parse)

Only stdlib + requests + re + json are used. All HTTP for the North Data auth
session and page fetches is done with `requests` directly (NOT self.http_get),
because the PHP northdataGet uses its own User-Agent, manual `Cookie: auth=...`
header, follow-redirects and a 30s timeout that must be reproduced exactly.
"""
from __future__ import annotations

import html as _html
import json
import re
import time
import urllib.parse

import requests


class NorthDataMixin:
    # ── tiny PHP-equivalent helpers ─────────────────────────────────────────
    @staticmethod
    def _hedecode(s: str) -> str:
        """PHP html_entity_decode()."""
        return _html.unescape(s)

    @staticmethod
    def _strip_tags(s: str) -> str:
        """PHP strip_tags() — remove tags, do NOT decode entities."""
        return re.sub(r'<[^>]*>', '', s)

    @staticmethod
    def _ucfirst(s: str) -> str:
        return s[:1].upper() + s[1:]

    def _nd_log(self, entry: dict) -> None:
        """Append to self.log ($this->log[] = ...). ToolBase does not create
        self.log, so create it lazily to stay robust when this mixin is used
        standalone; the composed class normally provides it."""
        if not hasattr(self, 'log') or self.log is None:
            self.log = []
        self.log.append(entry)

    # ── auth session ────────────────────────────────────────────────────────
    def _get_northdata_auth_cookie(self):
        """Port of getNorthdataAuthCookie(). Logs in once via the RPC endpoint,
        caches the 'auth' cookie value on self.northdata_auth_cookie and reuses it.
        A cached empty string means "login already failed" -> return None."""
        if self.northdata_auth_cookie is not None:
            return self.northdata_auth_cookie or None

        email = self.config.get('northdata_email') or ''
        password = self.config.get('northdata_password') or ''
        if not email or not password:
            self._progress('northdata', "NorthData auth: no credentials configured")
            self.northdata_auth_cookie = ''
            return None

        http_code = 0
        curl_error = ''
        response_headers = ''
        try:
            r = requests.post(
                'https://www.northdata.com/rpc.json/user/login',
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                data=urllib.parse.urlencode({'email': email, 'password': password}),
                timeout=15,
                allow_redirects=False,
            )
            http_code = r.status_code
            # Reconstruct the raw Set-Cookie header(s) so the exact PHP regex
            # (`set-cookie:\s*auth=([^;]+)`) matches one cookie per line rather
            # than requests' comma-merged Set-Cookie value.
            set_cookie_lines = []
            raw = getattr(r, 'raw', None)
            if raw is not None and getattr(raw, 'headers', None) is not None \
                    and hasattr(raw.headers, 'getlist'):
                set_cookie_lines = raw.headers.getlist('Set-Cookie')
            if not set_cookie_lines and 'Set-Cookie' in r.headers:
                set_cookie_lines = [r.headers['Set-Cookie']]
            response_headers = "\r\n".join("set-cookie: " + c for c in set_cookie_lines)
        except requests.RequestException as e:
            http_code = 0
            curl_error = str(e)

        m = re.search(r'set-cookie:\s*auth=([^;]+)', response_headers, re.I)
        if m:
            self.northdata_auth_cookie = m.group(1)
            self._progress('northdata', "NorthData auth: logged in successfully")
            return self.northdata_auth_cookie

        self._progress('northdata',
                       "NorthData auth: login failed (HTTP {}{})".format(
                           http_code, (", " + curl_error) if curl_error else ""))
        self.northdata_auth_cookie = ''
        return None

    def _northdata_get(self, url: str):
        """Port of northdataGet(): authenticated GET with manual Cookie header."""
        auth_cookie = self._get_northdata_auth_cookie()
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        if auth_cookie:
            headers['Cookie'] = "auth={}".format(auth_cookie)
        self._nd_log({'tool': 'northdata_get', 'input': url,
                      'output': 'auth=' + ('yes' if auth_cookie else 'no')})
        try:
            r = requests.get(url, headers=headers, allow_redirects=True, timeout=30)
        except requests.RequestException:
            return None
        http_code = r.status_code
        result = r.text
        if http_code != 200 or not result:
            return None
        return result

    # ── search_northdata ─────────────────────────────────────────────────────
    def search_northdata(self, entity_name: str) -> str:
        # Clean parenthetical content
        clean = re.sub(r'\([^)]*\)', '', entity_name).strip()

        # Strategy 1: Direct URL — NorthData resolves company names to their pages
        direct_url = "https://www.northdata.com/" + urllib.parse.quote_plus(clean)
        direct_html = self._northdata_get(direct_url)
        if direct_html and len(direct_html) > 2000:
            is_company_page = False
            page_title = ''
            title_match = re.search(r'<title>([^<]+)</title>', direct_html, re.I)
            if title_match:
                page_title = title_match.group(1)
                first_word = clean.split(' ')[0]
                is_company_page = ('Search for' not in page_title) \
                    and (',' in page_title) \
                    and (clean.lower() in page_title.lower()
                         or first_word.lower() in page_title.lower())

            if is_company_page:
                page_text = self._parse_northdata_html(direct_html)
                result = page_text
                self._nd_log({'tool': 'search_northdata', 'input': entity_name,
                              'output': result[:500]})
                return result

        # Strategy 2: Browserbase search (JS-rendered results)
        parsed = self._northdata_search_parse(clean)
        if not parsed:
            stripped = re.sub(r',?\s*\b(S\.A\.?|GmbH|AG|B\.V\.?|Ltd\.?|Inc\.?|LLC|Oyj|Oy|AB|AS|ApS)\s*$',
                              '', clean, flags=re.I)
            stripped = stripped.strip()
            if stripped != clean:
                parsed = self._northdata_search_parse(stripped)

        if not parsed:
            result = "No North Data results found."
            self._nd_log({'tool': 'search_northdata', 'input': entity_name, 'output': result})
            return result

        # List all matches
        match_list = []
        for r in parsed:
            match_list.append("{} → {}".format(r['name'], r['url']))
        header = "=== NorthData Search Results ===\n" + "\n".join(match_list)

        # Fetch full company page for the best (first) match
        best_url = parsed[0]['url']
        page_html = self._northdata_get(best_url)
        page_text = ''
        if page_html:
            page_text = self._parse_northdata_html(page_html)

        result = header
        if page_text:
            result += "\n\n{}".format(page_text)

        self._nd_log({'tool': 'search_northdata', 'input': entity_name, 'output': result[:500]})
        return result

    # ── parseNorthdataHtml ────────────────────────────────────────────────────
    def _parse_northdata_html(self, html: str) -> str:
        md = []

        # === Company Info ===
        # Title gives us: "Scanfil Oyj, Sievi, Finland, PRH 2422742-9: Network, ..."
        company_name = ''
        m = re.search(r'<title>([^<]+)</title>', html, re.I)
        if m:
            title_parts = m.group(1).split(':')
            company_name = title_parts[0].strip()

        # Extract name variants from the company info section (unused downstream —
        # kept for fidelity with the PHP).
        names = []
        alias_matches = re.findall(r'<div[^>]*class="[^"]*alias[^"]*"[^>]*>([^<]+)<', html, re.I)
        if alias_matches:
            names = [a.strip() for a in alias_matches]
        name_block = re.search(r'<div[^>]*class="[^"]*company-name[^"]*"[^>]*>(.*?)</div>',
                               html, re.I | re.S)
        if name_block:
            for n in re.findall(r'<[^>]+>([^<]{2,})</[^>]+>', name_block.group(1), re.I):
                n = self._hedecode(n).strip()
                if n and n not in names:
                    names.append(n)

        # Registry IDs (unused downstream — kept for fidelity)
        registry_ids = []
        reg_matches = re.findall(
            r'(?:PRH|HRB|Siren|CVR|KVK|RIK|Registro Mercantil|Companies House|ON)\s*[\w\d\-]+',
            html, re.I)
        if reg_matches:
            seen = set()
            registry_ids = [x for x in reg_matches if not (x in seen or seen.add(x))]

        # LEI (unused downstream — kept for fidelity)
        lei = ''
        lei_match = re.search(r'LEI[^>]*>.*?([A-Z0-9]{20})', html, re.I | re.S)
        if lei_match:
            lei = lei_match.group(1)
        else:
            re.search(r'\b([A-Z0-9]{20})\b', html)  # intentionally unused (as in PHP)

        # Address (unused downstream — kept for fidelity)
        address = ''
        addr_match = re.search(r'<span[^>]*class="[^"]*address[^"]*"[^>]*>(.*?)</span>',
                               html, re.I | re.S)
        if addr_match:
            address = self._strip_tags(addr_match.group(1)).strip()

        # Corporate purpose (unused downstream — kept for fidelity)
        purpose = ''
        purp_match = re.search(r'Corporate purpose.*?<p[^>]*>(.*?)</p>', html, re.I | re.S)
        if purp_match:
            purpose = self._strip_tags(self._hedecode(purp_match.group(1))).strip()

        # Status
        status = 'Active'
        sm = re.search(r'title="(in liquidation|terminated|dissolved)"', html, re.I)
        if sm:
            status = self._ucfirst(sm.group(1).strip())

        # Build header from the plain-text version of the info section
        text = self.html_to_text(html)
        lines = text.split("\n")

        info_fields = self._extract_northdata_info_fields(lines)

        md.append("## {} — NorthData".format(info_fields['name']))
        md.append('')
        md.append("**Name:** {}".format(info_fields['name']))
        if info_fields['also_known_as']:
            md.append("**Also known as:** " + ", ".join(info_fields['also_known_as']))
        if info_fields['registry_id']:
            md.append("**Registry ID:** {}".format(info_fields['registry_id']))
        if info_fields['lei']:
            md.append("**LEI:** {}".format(info_fields['lei']))
        if info_fields['address']:
            md.append("**Address:** {}".format(info_fields['address']))
        if info_fields['country']:
            md.append("**Country:** {}".format(info_fields['country']))
        md.append("**Status:** {}".format(status))
        if info_fields['industry']:
            md.append("**Industry:** {}".format(info_fields['industry']))
        if info_fields['purpose']:
            md.append("**Corporate purpose:** {}".format(info_fields['purpose']))

        # === Network Graph ===
        network = self._extract_northdata_network(html)
        if network:
            md.append('')
            md.append(network)

        # === Financials ===
        financials = self._extract_northdata_financials(html)
        if financials:
            md.append('')
            md.append(financials)

        # === Publications (trademarks, shareholdings) ===
        pubs = self._extract_northdata_publications(lines)
        if pubs:
            md.append('')
            md.append(pubs)

        return "\n".join(md)

    # ── extractNorthdataInfoFields ────────────────────────────────────────────
    def _extract_northdata_info_fields(self, lines: list) -> dict:
        fields = {
            'name': '', 'also_known_as': [], 'registry_id': '',
            'lei': '', 'address': '', 'country': '',
            'industry': '', 'purpose': '',
        }

        section = None
        name_lines = []
        for line in lines:
            t = line.strip()

            # Detect section headers
            if t == 'Name':
                section = 'name'
                continue
            if t == 'Identification':
                section = 'id'
                continue
            if t == 'Address':
                section = 'address'
                continue
            if t == 'Corporate purpose':
                section = 'purpose'
                continue
            if t in ('Financial performance', 'History', 'Network', 'Legal Structure',
                     'Financials', 'Publications'):
                section = None
                continue

            if section == 'name' and t:
                # Skip noise
                if re.match(r'(Dossier|Watch|Premium|Upgrade|Learn more|Set watch|Cancel|Create dossier|STAY UP)',
                            t, re.I):
                    continue
                if 'maximum number of watches' in t:
                    continue
                if 'subscription plan' in t:
                    continue
                if 'printable PDF' in t:
                    continue
                if 'email address' in t:
                    continue
                if 'feature is only available' in t:
                    continue
                if 'Subscribe to our newsletter' in t:
                    continue
                # Skip language prefixes like "(englanti):"
                if re.match(r'^\([a-z]+\):\s*', t, re.I):
                    t = re.sub(r'^\([a-z]+\):\s*', '', t, flags=re.I)
                name_lines.append(t)

            if section == 'id' and t:
                # Label-only lines — skip
                if re.match(r'^(Bis|Lei|Cvrcom|EUID|Siret)$', t, re.I):
                    continue
                # Registry ID value: "PRH 2422742-9", "HRB 6684", etc.
                rm = re.match(r'^(PRH|HRB|Siren|CVR|KVK|RIK|ON|Registro Mercantil|Companies House)\s+[\w\d\-]+',
                              t, re.I)
                if rm:
                    if not fields['registry_id']:
                        fields['registry_id'] = rm.group(0)
                # LEI (20 alphanumeric chars)
                if re.match(r'^[A-Z0-9]{20}$', t):
                    fields['lei'] = t
                # EUID value
                if re.match(r'^[A-Z]{2}[A-Z]+\.\d[\d\-]+$', t):
                    pass  # EUID like FIFPRO.2422742-9 — skip, not needed

            if section == 'address' and t:
                if not fields['address']:
                    fields['address'] = t
                    # Extract country from address (last word after last comma)
                    parts = [p.strip() for p in t.split(',')]
                    last_part = parts[-1]
                    if re.match(r'^(Finland|Germany|France|Netherlands|Austria|Switzerland|Belgium|Luxembourg|Italy|Spain|Denmark|Sweden|Norway|Poland|Czech Republic|Ireland|Estonia|United Kingdom)$',
                                last_part, re.I):
                        fields['country'] = last_part

            if section == 'purpose' and t:
                # First line is the NACE code (e.g. "26.11.0")
                if not fields['industry'] and re.match(r'^\d+\.\d+', t):
                    fields['industry'] = t
                    continue
                # Second line is the short industry description
                if fields['industry'] and '—' not in fields['industry'] \
                        and not re.match(r'^\d+\.\d+', t) and len(t) > 3:
                    fields['industry'] += ' — ' + t
                    continue
                # Remaining lines are the full corporate purpose
                if len(t) > 20:
                    fields['purpose'] = (fields['purpose'] + ' ' + t) if fields['purpose'] else t

        # Process name lines — deduplicate
        if name_lines:
            fields['name'] = name_lines[0]
            aka = []
            for n in name_lines[1:]:
                if n.lower() != fields['name'].lower() \
                        and n.lower() not in [a.lower() for a in aka]:
                    aka.append(n)
            fields['also_known_as'] = aka

        # If industry has a code but no description, check the next purpose line
        if fields['industry'] and '—' not in fields['industry']:
            industry_found = False
            for line in lines:
                t = line.strip()
                if industry_found and len(t) > 5 and not re.match(r'^\d', t):
                    fields['industry'] += ' — ' + t
                    break
                if t == fields['industry']:
                    industry_found = True

        return fields

    # ── extractNorthdataNetwork ───────────────────────────────────────────────
    def _extract_northdata_network(self, html: str) -> str:
        nodes = {}
        node_matches = re.findall(
            r'class="node"[^>]*data-id=(?:3D)?"(\d+)"[^>]*data-text=(?:3D)?"([^"]+)"[^>]*data-type=(?:3D)?"([cp])"[^>]*data-description=(?:3D)?"([^"]*)"[^>]*data-root=(?:3D)?"([^"]*)"[^>]*data-old=(?:3D)?"([^"]*)"',
            html, re.I)
        if node_matches:
            for nm in node_matches:
                nodes[nm[0]] = {
                    'name': self._hedecode(self._decode_mhtml(nm[1])),
                    'type': nm[2],  # c=company, p=person
                    'description': self._hedecode(self._decode_mhtml(nm[3])),
                    'root': (nm[4] == 'true' or nm[4] == '3Dtrue'),
                    'old': (nm[5] == 'true' or nm[5] == '3Dtrue' or bool(nm[5])),
                }

        # Try simpler pattern if MHTML encoding differs
        if not nodes:
            node_matches2 = re.finditer(
                r'class="node"[^>]*data-id="(\d+)"[^>]*data-text="([^"]+)"[^>]*data-type="([cp])"',
                html, re.I)
            for nm in node_matches2:
                full = nm.group(0)
                desc = ''
                dm = re.search(r'data-description="([^"]*)"', full)
                if dm:
                    desc = dm.group(1)
                root = 'data-root="true"' in full
                old = 'data-old="true"' in full
                nodes[nm.group(1)] = {
                    'name': self._hedecode(nm.group(2)),
                    'type': nm.group(3),
                    'description': self._hedecode(desc),
                    'root': root,
                    'old': old,
                }

        if not nodes:
            return ''

        # Extract edges
        edges = []
        for em in re.finditer(
                r'data-source-id=(?:3D)?"(\d+)"[^>]*data-target-id=(?:3D)?"(\d+)"[^>]*data-description=(?:3D)?"([^"]+)"',
                html, re.I):
            full = em.group(0)
            old = ('data-old=3D"true"' in full) or ('data-old="true"' in full)
            edges.append({
                'source': em.group(1),
                'target': em.group(2),
                'description': self._hedecode(self._decode_mhtml(em.group(3))),
                'old': old,
            })

        # Find root node
        root_id = None
        for _id, node in nodes.items():
            if node['root']:
                root_id = _id
                break
        if not root_id:
            return ''

        # Categorise relationships from root
        subsidiaries = []
        people = []
        sub_subsidiaries = []

        for edge in edges:
            source_node = nodes.get(edge['source'])
            target_node = nodes.get(edge['target'])
            if not source_node or not target_node:
                continue

            # Edges FROM root node
            if edge['source'] == root_id:
                if target_node['type'] == 'c':
                    subsidiaries.append({
                        'name': target_node['name'],
                        'location': target_node['description'],
                        'relationship': edge['description'],
                        'old': edge['old'],
                    })
                elif target_node['type'] == 'p':
                    people.append({
                        'name': target_node['name'],
                        'location': target_node['description'],
                        'role': edge['description'],
                        'old': edge['old'],
                    })
            # Edges from subsidiaries (sub-subsidiaries or people at subsidiaries)
            elif source_node['type'] == 'c' and edge['source'] != root_id:
                if target_node['type'] == 'c':
                    sub_subsidiaries.append({
                        'parent': source_node['name'],
                        'name': target_node['name'],
                        'location': target_node['description'],
                        'relationship': edge['description'],
                        'old': edge['old'],
                    })
                # Person roles at subsidiaries — add as note to existing person
                if target_node['type'] == 'p':
                    for p in people:
                        if p['name'] == target_node['name']:
                            p.setdefault('also', []).append(
                                edge['description'] + ' at ' + source_node['name'])

        md = []
        md.append('### Network — Corporate Structure')

        # Current subsidiaries
        current = [s for s in subsidiaries if not s['old']]
        previous = [s for s in subsidiaries if s['old']]

        if current:
            md.append('**Subsidiaries (current):**')
            for sub in current:
                md.append("- {} — {}".format(sub['location'], sub['relationship']))
                for ss in sub_subsidiaries:
                    if ss['parent'] == sub['name']:
                        md.append("  - {} — {}".format(ss['location'], ss['relationship']))
        if previous:
            md.append('**Subsidiaries (previous):**')
            for sub in previous:
                md.append("- {} — {}".format(sub['location'], sub['relationship']))
                for ss in sub_subsidiaries:
                    if ss['parent'] == sub['name']:
                        md.append("  - {} — {}".format(ss['location'], ss['relationship']))

        # People
        current_people = [p for p in people if not p['old']]
        if current_people:
            md.append('')
            md.append('### Network — People')
            for p in current_people:
                # Clean location to just city/country
                loc = re.sub(r'^' + re.escape(p['name']) + r',\s*', '', p['location'], flags=re.I)
                line = "- {} ({}) — {}".format(p['name'], loc, p['role'])
                md.append(line)
                if p.get('also'):
                    for also in p['also']:
                        md.append("  - Also: {}".format(also))

        return "\n".join(md)

    # ── decodeMhtml ───────────────────────────────────────────────────────────
    def _decode_mhtml(self, s: str) -> str:
        """Decode MHTML quoted-printable (=C3=B6 -> ö etc.). PHP operates on byte
        strings; here we rebuild the byte sequence (each =XX or ASCII char becomes
        one byte) and decode UTF-8 so the eventual unicode output matches PHP's
        UTF-8 rendering. Falls back to the raw substitution if that is not valid."""
        decoded = re.sub(r'=([0-9A-Fa-f]{2})', lambda m: chr(int(m.group(1), 16)), s)
        try:
            return decoded.encode('latin-1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return decoded

    # ── extractNorthdataFinancials ────────────────────────────────────────────
    def _extract_northdata_financials(self, html: str) -> str:
        matches = re.findall(r'data-data="([^"]{100,})"', html, re.S)
        if not matches:
            return ''

        year_data = {}   # metric => {year => formattedValue}
        all_years = {}

        for raw in matches:
            decoded = self._hedecode(self._decode_mhtml(raw))
            try:
                jsonv = json.loads(decoded)
            except (ValueError, TypeError):
                continue
            if not jsonv:
                continue

            # Format 1: item[] with title and data.data[]
            if isinstance(jsonv, dict) and isinstance(jsonv.get('item'), list):
                for item in jsonv['item']:
                    metric = item.get('title', '') if isinstance(item, dict) else ''
                    data_points = (item.get('data', {}) or {}).get('data', []) \
                        if isinstance(item, dict) else []
                    if not metric or not isinstance(data_points, list):
                        continue
                    for dp in data_points:
                        year = dp.get('year', '') if isinstance(dp, dict) else ''
                        val = dp.get('formattedValue', '') if isinstance(dp, dict) else ''
                        if year and val:
                            year_data.setdefault(metric, {})[year] = val
                            all_years[year] = True

            # Format 2: financials[] with date and items[]
            if isinstance(jsonv, dict) and isinstance(jsonv.get('financials'), list):
                for fy in jsonv['financials']:
                    if not isinstance(fy, dict):
                        continue
                    year = (fy.get('date', '') or '')[0:4]
                    if not year:
                        continue
                    all_years[year] = True
                    for item in (fy.get('items') or []):
                        if not isinstance(item, dict):
                            continue
                        metric = item.get('name', '')
                        val = item.get('formattedValue', '')
                        if metric and val:
                            year_data.setdefault(metric, {})[year] = val

        if not year_data or not all_years:
            return ''

        # Take most recent 3 years
        years = sorted(all_years.keys())
        years = years[-3:]

        metric_order = ['Revenue', 'Earnings', 'Total assets', 'Equity', 'Equity ratio',
                        'Return on equity', 'Return on sales', 'Taxes', 'Cash on hand',
                        'Receivables', 'Liabilities', 'Employee number',
                        'Revenue per employee', 'Base/share capital', 'Real estate']

        md = []
        md.append('### Financials')
        header = '| Metric | ' + ' | '.join(years) + ' |'
        sep = '|---|' + '|'.join(['---'] * len(years)) + '|'
        md.append(header)
        md.append(sep)

        for metric in metric_order:
            if metric not in year_data:
                continue
            vals = []
            for y in years:
                vals.append(year_data[metric].get(y, '—'))
            md.append('| ' + metric + ' | ' + ' | '.join(vals) + ' |')

        return "\n".join(md)

    # ── extractNorthdataPublications ──────────────────────────────────────────
    def _extract_northdata_publications(self, lines: list) -> str:
        trademarks = []
        relationships = []
        in_pubs = False
        in_mentions = False

        for line in lines:
            t = line.strip()
            if t == 'Publications':
                in_pubs = True
                in_mentions = False
                continue
            if t == 'Mentions':
                in_mentions = True
                in_pubs = False
                continue
            if t in ('', 'Premium', 'Loading network',
                     'There are no publications matching your search.'):
                continue
            if re.match(r'^(Upgrade|Learn more|€\d|Premium plans|STAY UP|Subscribe)', t, re.I):
                continue

            if in_pubs and t:
                # Skip dates, balance sheets
                if re.match(r'^\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}$',
                            t, re.I):
                    continue
                if re.search(r'Balance sheet|Earnings statement', t, re.I):
                    continue
                if re.match(r'^via$', t, re.I):
                    continue
                # Trademarks
                if re.search(r'mark:\s*"([^"]+)"', t, re.I):
                    trademarks.append("- {}".format(t))
                # Shareholdings
                if re.search(r'Shareholding:', t, re.I):
                    relationships.append("- {}".format(t))

            if in_mentions and t:
                if re.match(r'^\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}$',
                            t, re.I):
                    continue
                if re.match(r'^(in|also|via)$', t, re.I):
                    continue
                if re.match(r'^(Ler|Hrdk|Rne)$', t, re.I):
                    continue
                if re.match(r'^\d{2}/\d{2}/\d{4}$', t):
                    continue
                if re.search(r'Direct parent:|Ultimate parent|Shareholding:', t, re.I):
                    relationships.append("- {}".format(t))
                if re.search(r'Liste der Gesellschafter|Gesellschafterliste', t, re.I):
                    relationships.append("- {}".format(t))

        md = []

        if relationships:
            # Deduplicate (preserve first-seen order)
            seen = set()
            relationships = [x for x in relationships if not (x in seen or seen.add(x))]
            md.append('### Subsidiaries & Relationships')
            md.extend(relationships)

        if trademarks:
            seen = set()
            trademarks = [x for x in trademarks if not (x in seen or seen.add(x))]
            md.append('')
            md.append('### Trademarks')
            md.extend(trademarks)

        return "\n".join(md)

    # ── country-name map / northdataSearchParse ───────────────────────────────
    NORTHDATA_COUNTRY_NAMES = {
        'DE': 'Germany', 'NL': 'Netherlands', 'FR': 'France', 'AT': 'Austria',
        'CH': 'Switzerland', 'BE': 'Belgium', 'LU': 'Luxembourg', 'IT': 'Italy',
        'ES': 'Spain', 'DK': 'Denmark', 'SE': 'Sweden', 'NO': 'Norway',
        'FI': 'Finland', 'PL': 'Poland', 'CZ': 'Czech Republic', 'IE': 'Ireland',
        'GB': 'United Kingdom',
    }

    def _northdata_search_parse(self, query: str) -> list:
        url = "https://www.northdata.com/search?query=" + urllib.parse.quote_plus(query)
        # NorthData search results are JS-rendered — need Browserbase.
        html = self.browserbase_fetch_html(url)
        if not html:
            return []

        results = []
        matches = re.finditer(r'<a[^>]*href="(/[^"]+)"[^>]*>([^<]+)</a>', html, re.I)
        seen = {}
        for match in matches:
            href = match.group(1)
            text = self._hedecode(match.group(2)).strip()
            if len(text) < 5 or len(text) > 120:
                continue
            if ',' not in text:
                continue
            if href.startswith('/search'):
                continue
            if href.startswith('/_'):
                continue
            if href.startswith('/?'):
                continue
            # Company URLs have 2+ path segments (/Name/RegistryID)
            path_segments = href.strip('/').split('/')
            if len(path_segments) < 2:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen[key] = True
            results.append({
                'name': text,
                'url': "https://www.northdata.com{}".format(href),
            })
            if len(results) >= 15:
                break
        return results

    # ── validate_northdata_entity ─────────────────────────────────────────────
    def validate_northdata_entity(self, entity_name: str, registry_id: str,
                                  country_code: str = ''):
        clean = re.sub(r'\([^)]*\)', '', entity_name).strip()
        country_name = self.NORTHDATA_COUNTRY_NAMES.get(country_code.upper(), '')

        # Strategy 1: Direct URL
        direct_url = "https://www.northdata.com/" + urllib.parse.quote_plus(clean)
        direct_html = self._northdata_get(direct_url)
        if direct_html and len(direct_html) > 2000:
            is_company_page = False
            page_title = ''
            title_match = re.search(r'<title>([^<]+)</title>', direct_html, re.I)
            if title_match:
                page_title = title_match.group(1)
                is_company_page = ('Search for' not in page_title) and (',' in page_title)

            if is_company_page:
                # Extract name from title
                title_name = page_title.split(':')[0]
                country_match = (country_name.lower() in title_name.lower()) if country_name else None
                if registry_id:
                    title_name = re.sub(r',?\s*' + re.escape(registry_id) + r'\s*$', '', title_name)
                if country_name:
                    title_name = re.sub(r',?\s*' + re.escape(country_name) + r'\s*$', '',
                                        title_name, flags=re.I)
                # Strip city (last comma-segment)
                title_name = re.sub(r',\s*[^,]+\s*$', '', title_name)

                # Status from heading suffix
                status = None
                sm = re.search(r'title="(in liquidation|terminated|dissolved)"', direct_html, re.I)
                if sm:
                    status = sm.group(1).strip()
                elif re.search(r'class="heading"[^>]*>\s*[^<]+', direct_html, re.I):
                    status = 'active'

                registry_id_on_page = registry_id.lower() in direct_html.lower()

                best = {
                    'name': title_name.strip(),
                    'url': direct_url,
                    'country_match': country_match,
                    'status': status,
                    'registry_id_match': registry_id_on_page,
                }
                self._nd_log({'tool': 'validate_northdata_entity',
                              'input': "{} / {} / {}".format(entity_name, registry_id, country_code),
                              'output': json.dumps(best, separators=(',', ':'))})
                return best

        # Strategy 2: Browserbase search — try full name, then stripped suffix
        results = self._northdata_search_parse(clean)

        has_country_match = False
        if country_name:
            for r in results:
                if country_name.lower() in r['name'].lower():
                    has_country_match = True
                    break

        if not results or not has_country_match:
            stripped = re.sub(r',?\s*\b(S\.?L\.?|S\.A\.?|GmbH|AG|B\.V\.?|Ltd\.?|Inc\.?|LLC|S\.?R\.?L\.?|Oyj|Oy|AB|AS|ApS)\s*$',
                              '', clean, flags=re.I)
            stripped = stripped.strip()
            if stripped != clean:
                fallback = self._northdata_search_parse(stripped)
                if fallback:
                    results = fallback

        if not results:
            return None

        # Match on country name in the result text
        best = None
        if country_name:
            for r in results:
                if country_name.lower() in r['name'].lower():
                    best = dict(r)
                    best['country_match'] = True
                    break

        # Fallback to first result
        if not best:
            best = dict(results[0])
            best['country_match'] = False if country_name else None

        # Fetch company page — extract status and check registry ID appears on page
        page_html = self._northdata_get(best['url'])
        status = None
        registry_id_on_page = False
        if page_html:
            sm = re.search(r'title="(in liquidation|terminated|dissolved)"', page_html, re.I)
            if sm:
                status = sm.group(1).strip()
            elif re.search(r'class="heading"[^>]*>\s*[^<]+', page_html, re.I):
                status = 'active'
            registry_id_on_page = registry_id.lower() in page_html.lower()
        best['status'] = status
        best['registry_id_match'] = registry_id_on_page

        self._nd_log({'tool': 'validate_northdata_entity',
                      'input': "{} / {} / {}".format(entity_name, registry_id, country_code),
                      'output': json.dumps(best, separators=(',', ':'))})
        return best

    # ── northdata_network (Browserbase, inline WebDriver) ─────────────────────
    def northdata_network(self, northdata_url: str) -> str:
        api_key = self.config.get('browserbase_api_key') or ''
        project_id = self.config.get('browserbase_project_id') or ''
        if not api_key or not project_id:
            return "Error: Browserbase not configured."

        selenium_base = 'http://connect.usw2.browserbase.com/webdriver'

        # Create Browserbase session
        try:
            bb_session = requests.post(
                'https://api.browserbase.com/v1/sessions',
                headers={'x-bb-api-key': api_key, 'Content-Type': 'application/json'},
                data=json.dumps({'projectId': project_id}),
                timeout=30).json()
        except (requests.RequestException, ValueError):
            bb_session = None
        bb_session_id = (bb_session or {}).get('id', '') if isinstance(bb_session, dict) else ''
        if not bb_session_id:
            return "Error: Could not create Browserbase session."

        # Create WebDriver session
        try:
            wd = requests.post(
                selenium_base + '/session',
                headers={'Content-Type': 'application/json', 'x-bb-api-key': api_key,
                         'session-id': bb_session_id},
                data=json.dumps({'capabilities': {'alwaysMatch': {'browserName': 'chrome'}}}),
                timeout=30).json()
        except (requests.RequestException, ValueError):
            wd = None
        wd_session_id = ((wd or {}).get('value', {}) or {}).get('sessionId', '') \
            if isinstance(wd, dict) else ''
        if not wd_session_id:
            return "Error: Could not create WebDriver session."

        # Navigate to the NorthData page
        try:
            requests.post(
                selenium_base + "/session/{}/url".format(wd_session_id),
                headers={'Content-Type': 'application/json', 'x-bb-api-key': api_key,
                         'session-id': bb_session_id},
                data=json.dumps({'url': northdata_url}),
                timeout=30)
        except requests.RequestException:
            pass

        # Wait for JS to render the network graph
        time.sleep(10)

        # Get rendered page source
        try:
            resp = requests.get(
                selenium_base + "/session/{}/source".format(wd_session_id),
                headers={'x-bb-api-key': api_key, 'session-id': bb_session_id},
                timeout=30)
            data = resp.json()
        except (requests.RequestException, ValueError):
            data = None
        html = (data or {}).get('value', '') if isinstance(data, dict) else ''

        if not html:
            return "Error: Could not get rendered page from Browserbase."

        # Extract the network SVG
        svg_match = re.search(r'<svg[^>]*aria-label="Network"[^>]*>(.*?)</svg>', html, re.S)
        if not svg_match:
            self._nd_log({'tool': 'northdata_network', 'input': northdata_url,
                          'output': 'No network graph found on page.'})
            return "No network graph found on this NorthData page."
        svg = svg_match.group(1)

        # Extract nodes
        all_nodes = re.findall(r'<a[^>]*class="node"[^>]*>', svg)
        node_map = {}
        for node_tag in all_nodes:
            id_m = re.search(r'data-id="(\d+)"', node_tag)
            text_m = re.search(r'data-text="([^"]+)"', node_tag)
            desc_m = re.search(r'data-description="([^"]+)"', node_tag)
            root_m = re.search(r'data-root="([^"]*)"', node_tag)
            _id = id_m.group(1) if id_m else '?'
            node_map[_id] = {
                'text': self._hedecode(text_m.group(1) if text_m else '?'),
                'desc': self._hedecode(desc_m.group(1) if desc_m else ''),
                'root': (root_m.group(1) if root_m else '') != '',
            }

        # Find the root entity
        root_id = None
        root_name = ''
        root_desc = ''
        for _id, n in node_map.items():
            if n['root']:
                root_id = str(_id)
                root_name = n['text']
                root_desc = n['desc']
                break

        # Extract links
        links = re.findall(
            r'data-source-id="(\d+)"[^>]*data-target-id="(\d+)"[^>]*data-description="([^"]+)"',
            svg)

        # Build output
        lines = []
        lines.append("=== NorthData Network: {} ===".format(root_desc))
        lines.append("")
        lines.append("Entity: {} [ROOT]".format(root_name))
        lines.append("")

        # Categorise relationships
        owned_by = []
        owns = []
        other = []

        for l in links:
            src_id = l[0]
            tgt_id = l[1]
            desc = self._hedecode(l[2])
            src_name = node_map.get(src_id, {}).get('text', "ID:{}".format(src_id))
            tgt_name = node_map.get(tgt_id, {}).get('text', "ID:{}".format(tgt_id))
            tgt_desc = node_map.get(tgt_id, {}).get('desc', '')
            src_desc = node_map.get(src_id, {}).get('desc', '')

            has_parent_word = re.search(r'\b(parent|Ultimate parent|Direct parent)\b', desc, re.I)
            has_stake = re.search(r'(?:≥\s*)?\d+%|Control|Profit Transfer', desc, re.I)
            is_ownership = bool(has_parent_word or has_stake)

            if src_id == root_id and is_ownership:
                owned_by.append("  {} ({}): {}".format(tgt_name, tgt_desc, desc))
            elif tgt_id == root_id and is_ownership:
                owns.append("  {} ({}): {}".format(src_name, src_desc, desc))
            else:
                other.append("  {} → {}: {}".format(src_name, tgt_name, desc))

        if owned_by:
            lines.append("OWNED BY (parent entities):")
            for l in owned_by:
                lines.append(l)
            lines.append("")

        if owns:
            lines.append("Subsidiaries/controlled entities:")
            for l in owns:
                lines.append(l)
            lines.append("")

        if other:
            lines.append("Other relationships:")
            for l in other:
                lines.append(l)
            lines.append("")

        # Conclusion
        if owned_by:
            lines.append("Conclusion: {} has parent/owner entities above it (see OWNED BY section). "
                         "Consider contracting with the parent instead.".format(root_name))
        else:
            lines.append("Conclusion: {} appears to be the ultimate parent/TopCo — no parent entity "
                         "found above it in the network.".format(root_name))

        result = "\n".join(lines)
        self._nd_log({'tool': 'northdata_network', 'input': northdata_url, 'output': result})
        return result
