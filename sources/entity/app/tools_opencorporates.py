"""
Entity Lookup v3b (Python) — OpenCorporates tool cluster.

Faithful, like-for-like port of the OpenCorporates methods from php/tools.php
(class LookupTools):

    searchOpenCorporates       -> search_open_corporates
    openCorporatesDetail       -> open_corporates_detail
    ocFetchWithCaptcha         -> oc_fetch_with_captcha
    ocFetchWithScrapingBrowser -> oc_fetch_with_scraping_browser
    parseOpenCorporatesResults -> parse_open_corporates_results

Composed onto ToolBase (toolbase.py) via multiple inheritance. All shared
helpers (self.config, self._progress, self.html_to_text, self.http_get,
self._random_country, self.single_scraping_browser_fetch) live on the base.

IMPORTANT — despite the PHP docstring claiming "Uses 2Captcha to solve HAProxy
hCaptcha", the actual PHP implementation of ocFetchWithCaptcha does NOT touch
2Captcha at all: it fetches through the Bright Data Web Unlocker (which handles
CAPTCHAs server-side). config['twocaptcha_api_key'] is never referenced by any
of these methods. This port reproduces the real Bright Data logic, not the
docstring. See the report accompanying this file for details.

stdlib + requests only.
"""
from __future__ import annotations

import html as _html
import re
import time
from urllib.parse import quote_plus

import requests


class OpenCorporatesMixin:
    # ── OpenCorporates (Browserbase) ────────────────────────────────────────

    def _oc_log(self, entry: dict) -> None:
        """Append to self.log like PHP's `$this->log[] = [...]`.

        ToolBase does not (yet) declare self.log; the composed LookupTools class
        is expected to. Guard so the mixin is safe standalone.
        """
        if not hasattr(self, 'log') or self.log is None:
            self.log = []
        self.log.append(entry)

    def search_open_corporates(self, entity_name: str, jurisdiction_code: str | None = None) -> str:
        """Search OpenCorporates for a company name. Returns structured results
        from 140+ jurisdictions. Uses 2Captcha to solve HAProxy hCaptcha.
        """
        search_url = 'https://opencorporates.com/companies?q=' + quote_plus(entity_name) + '&type=companies'
        if jurisdiction_code:
            search_url += '&jurisdiction_code=' + quote_plus(jurisdiction_code)

        html = self.oc_fetch_with_captcha(search_url)

        if not html or len(html) < 200:
            result = "Error: OpenCorporates returned empty page (may be CAPTCHA-blocked)."
            self._oc_log({'tool': 'opencorporates', 'input': entity_name, 'output': result})
            return result

        # Check if still on CAPTCHA page
        if 'captcha' in html.lower() and '/companies/' not in html.lower():
            result = "Error: OpenCorporates CAPTCHA not solved — Browserbase could not bypass it."
            self._oc_log({'tool': 'opencorporates', 'input': entity_name, 'output': result})
            return result

        # Parse search results from HTML
        results = self.parse_open_corporates_results(html)

        if not results:
            result = f'No OpenCorporates results found for "{entity_name}".'
            self._oc_log({'tool': 'opencorporates', 'input': entity_name, 'output': result})
            return result

        lines = []
        for r in results:
            parts = [r['name']]
            if r['jurisdiction_name']:
                parts.append(r['jurisdiction_name'])
            elif r['jurisdiction']:
                parts.append(r['jurisdiction'])
            if r['company_number']:
                parts.append(f"#{r['company_number']}")
            if r['status']:
                parts.append(f"status: {r['status']}")
            if r['detailed_status']:
                parts.append(f"({r['detailed_status']})")
            if r['is_branch']:
                parts.append("BRANCH")
            if r['address']:
                parts.append(f"address: {r['address']}")
            if r['alternative_names']:
                parts.append("aka: " + ', '.join(r['alternative_names']))
            parts.append(f"url: {r['url']}")
            lines.append(' | '.join(parts))

        result = "\n".join(lines)
        self._oc_log({'tool': 'opencorporates', 'input': entity_name, 'output': result})
        return result

    def open_corporates_detail(self, jurisdiction: str, company_number: str) -> str:
        """Fetch full details for a single OpenCorporates company page."""
        url = f"https://opencorporates.com/companies/{jurisdiction}/{company_number}"
        html = self.oc_fetch_with_captcha(url)

        if not html or html.startswith('Error:'):
            result = html or "Error: OpenCorporates detail page returned empty."
            self._oc_log({'tool': 'opencorporates_detail',
                          'input': f"{jurisdiction}/{company_number}", 'output': result})
            return result

        text = self.html_to_text(html)
        self._oc_log({'tool': 'opencorporates_detail',
                      'input': f"{jurisdiction}/{company_number}", 'output': text})
        return text

    def oc_fetch_with_captcha(self, url: str) -> str:
        """Fetch OpenCorporates URL via Bright Data Web Unlocker.

        (PHP docstring claims 2Captcha; the real implementation is Bright Data.)
        """
        api_key = self.config.get('brightdata_api_key') or ''
        zone = self.config.get('brightdata_zone') or 'web_unlocker1'
        if not api_key:
            return "Error: Bright Data API key not configured."

        base_payload = {
            'zone': zone,
            'url': url,
            'format': 'raw',
            'headers': {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-GB,en;q=0.9',
            },
        }

        last_error = ''
        for attempt in range(3):
            base_payload['country'] = self._random_country()
            try:
                resp = requests.post(
                    'https://api.brightdata.com/request',
                    headers={
                        'Content-Type': 'application/json',
                        'Authorization': f"Bearer {api_key}",
                    },
                    json=base_payload,
                    timeout=60,
                )
            except requests.RequestException:
                # curl_exec() returned false; getinfo HTTP_CODE would be 0.
                html = None
                http_code = 0
                response_headers = []
            else:
                html = resp.text
                http_code = resp.status_code
                # Collect x-brd-* response headers (like the PHP HEADERFUNCTION).
                response_headers = [f"{k}: {v}" for k, v in resp.headers.items()
                                    if k.lower().startswith('x-brd-')]

            # Check for Bright Data error headers
            brd_error = ''
            for h in response_headers:
                if h.lower().startswith('x-brd-error:'):
                    brd_error = h[len('x-brd-error:'):].strip()
                    break

            if not brd_error and http_code == 200 and html and len(html) >= 200:
                return html

            last_error = brd_error or (f"HTTP {http_code}" if http_code != 200 else 'empty response')
            if attempt < 3:
                time.sleep(2)

        return f"Error: Bright Data Web Unlocker — {last_error} (after 3 attempts)"

    def oc_fetch_with_scraping_browser(self, url: str) -> str:
        """Fetch OpenCorporates URL via Bright Data Scraping Browser.

        Returns raw HTML or "Error: ..." string. Bypasses the normal text-length
        check since we need the HTML, not rendered text.

        PORT NOTE: the PHP shells out to `node scraping_browser.mjs <url> --json`
        (a Playwright CDP navigation) and reads {"html": ...} from stdout. That
        subprocess step is not reproducible with `requests`, so this delegates to
        self.single_scraping_browser_fetch(url) (the base's Playwright-over-CDP
        helper), then returns its raw HTML — matching the PHP's "return the HTML,
        not the text" contract. The base helper's own error strings are mapped
        onto the PHP's fixed error strings below.
        """
        ws = self.config.get('brightdata_scraping_browser_ws') or ''
        if not ws:
            return "Error: Scraping Browser not configured."

        text, raw_html = self.single_scraping_browser_fetch(url)
        # Base helper failed to navigate / returned an error -> no usable response.
        if raw_html is None:
            return "Error: Scraping Browser returned no response."

        html = raw_html or ''
        if len(html) < 200:
            return "Error: Scraping Browser returned empty page."

        return html

    def parse_open_corporates_results(self, html: str) -> list:
        results: list = []

        # Extract total count
        total_count = None
        cm = re.search(r'Found (\d+) compan', html, re.I)
        if cm:
            total_count = int(cm.group(1))  # noqa: F841  (parity with PHP; unused)

        # Each result is an <li class="search-result company ...">
        rows = re.findall(
            r'<li class=[\'"]search-result company([^"\']*)[\'"]>(.*?)</li>',
            html, re.I | re.S)
        if not rows:
            return []

        known_statuses = ['dissolved', 'deregistered', 'struck_off', 'removed', 'liquidated',
                          'registered', 'active', 'in_existence', 'live']

        for classes, content in rows:
            # Status from CSS classes
            is_branch = 'branch' in classes
            inactive = 'inactive' in classes
            class_words = re.split(r'\s+', classes.strip())
            # Detailed status is the last word(s) after active/inactive (e.g. dissolved, struck_off)
            detailed_status = None
            for w in class_words:
                if w in known_statuses and w != 'active' and w != 'inactive':
                    detailed_status = (w.replace('_', ' ')[:1].upper()
                                       + w.replace('_', ' ')[1:])

            # Company name and link
            link_match = re.search(
                r'<a[^>]+class="company_search_result[^"]*"[^>]+href="(/companies/([a-z_]+)/([^"]+))"[^>]*>([^<]+)</a>',
                content, re.I)
            if not link_match:
                continue
            href = link_match.group(1)
            jurisdiction = link_match.group(2)
            company_number = link_match.group(3)
            name = _html.unescape(link_match.group(4)).strip()
            name = name.strip('"')  # OC wraps some names in quotes

            # Skip non-company links
            if jurisdiction in ('search', 'users', 'events', 'statements'):
                continue

            # Jurisdiction display name from the title attribute on jurisdiction link
            jurisdiction_name = None
            jm = re.search(r'title="[^"]*(?:Data On|Companies In)\s+([^"]+?)\s*Companies?"', content)
            if jm:
                jurisdiction_name = jm.group(1).strip()
            else:
                jm = re.search(r'\(([A-Z][a-z][\w\s]+(?:\s*\([^)]+\))?),', content)
                if jm:
                    jurisdiction_name = jm.group(1).strip()

            # Address
            address = None
            am = re.search(r'<span class=[\'"]address[\'"]>(?:<a[^>]*>.*?</a>)?([^<]+)</span>',
                           content, re.I | re.S)
            if am:
                address = am.group(1).strip()

            # Alternative/previous names
            alt_names = []
            for an in re.findall(r'Previously/Alternatively known as ([^<]+)', content, re.I):
                alt_names.append(_html.unescape(an).strip())

            # Trademarks
            trademarks = []
            for t in re.findall(r'<span class=[\'"]slight_highlight[\'"]>([^<]+)</span>', content, re.I):
                t = _html.unescape(t).strip()
                # Filter out alt names already captured
                if not t.startswith('Previously') and t not in alt_names:
                    trademarks.append(t)

            active = (not inactive) and ('active' in classes or 'registered' in classes
                                         or 'in_existence' in classes or 'live' in classes)
            status = 'Inactive' if inactive else ('Active' if active else 'Unknown')

            results.append({
                'name': name,
                'jurisdiction': jurisdiction,
                'jurisdiction_name': jurisdiction_name,
                'company_number': company_number,
                'status': status,
                'detailed_status': detailed_status,
                'is_branch': is_branch,
                'address': address,
                'alternative_names': alt_names,
                'trademarks': trademarks,
                'url': f"https://opencorporates.com{href}",
            })

        return results
