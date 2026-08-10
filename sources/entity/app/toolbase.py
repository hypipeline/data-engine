"""
Entity Lookup v3b (Python) — shared tool foundation.

Faithful port of the shared/fetching parts of php/tools.php (class LookupTools):
the 4-level fetch cascade (curl -> Bright Data Web Unlocker -> Browserbase -> Wayback),
htmlToText, WHOIS, api-call counting, and the progress callback. Registry tool clusters
(SEC, Companies House, North Data, Delaware, Bizapedia, OpenCorporates, Google
Intelligence) are added as mixins in tools_*.py and composed in tools.py.

Everything is synchronous (like the PHP), using `requests`; the server runs a lookup in a
worker thread and streams the progress callback out over SSE.
"""
from __future__ import annotations

import html as _htmllib
import random
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

PROXY_COUNTRIES = [
    'al', 'ar', 'at', 'au', 'be', 'bg', 'br', 'ca', 'ch', 'cl', 'co', 'cr', 'cy', 'cz',
    'de', 'dk', 'ec', 'ee', 'es', 'fi', 'fr', 'gb', 'gr', 'hk', 'hr', 'hu', 'id', 'ie',
    'il', 'in', 'is', 'it', 'jp', 'ke', 'kr', 'lt', 'lu', 'lv', 'mk', 'mt', 'mx', 'my',
    'ng', 'nl', 'no', 'nz', 'pa', 'pe', 'ph', 'pk', 'pl', 'pt', 'ro', 'rs', 'se', 'sg',
    'si', 'sk', 'th', 'tr', 'tw', 'ua', 'us', 'uy', 'vn', 'za',
]

_BLOCK_RE = re.compile(
    r'access denied|security.*(issue|check)|captcha|403 error|unable to give you access',
    re.I)
_BLOCK_RE_SB = re.compile(
    r'access denied|security.*(issue|check)|captcha|403 error|unable to give you access|'
    r'service unavailable|dns failure', re.I)


class ToolBase:
    def __init__(self, config: dict, progress_callback=None):
        self.config = config
        self.progress_callback = progress_callback
        self.northdata_auth_cookie = None
        self.log = []          # PHP LookupTools::$log — tool call records (getLog())
        # Tool-use counting. api_calls = per-service totals of REAL calls; api_calls_detail =
        # per-operation ("service:op"); api_calls_cache = cache hits (not billed). Every call site
        # routes through self.count(), so nothing can silently skip counting.
        self.api_calls = {
            'claude': 0, 'browserbase': 0, 'brightdata': 0, 'openai': 0, 'bizapedia': 0, 'acra': 0,
            'sec': 0, 'companies_house': 0, 'delaware': 0, 'northdata': 0, 'opencorporates': 0,
            'nzco': 0, 'google': 0, 'linkedin': 0, 'yahoo': 0, 'http': 0, 'whois': 0,
            'wayback': 0, 'scraping_browser': 0,
        }
        self.api_calls_detail: dict = {}
        self.api_calls_cache: dict = {}

    # ── bookkeeping ─────────────────────────────────────────────────────────
    def get_api_calls(self) -> dict:
        return self.api_calls

    def get_log(self) -> list:
        return self.log

    # ── tool-use counting ───────────────────────────────────────────────────
    # Grouping for readable summaries. Anything not listed here is treated as a data SOURCE.
    _TRANSPORT_KEYS = {'browserbase', 'brightdata', 'scraping_browser', 'http', 'whois', 'wayback'}
    _LLM_KEYS = {'claude', 'openai'}

    def count(self, service: str, op: str | None = None, n: int = 1, cached: bool = False) -> None:
        """The single choke point for all tool-use counting. Records `n` real (or cached) calls to
        `service`, optionally tagged with an operation (recorded under `service:op`)."""
        if cached:
            self.api_calls_cache[service] = self.api_calls_cache.get(service, 0) + n
        else:
            self.api_calls[service] = self.api_calls.get(service, 0) + n
        if op:
            key = f"{service}:{op}"
            self.api_calls_detail[key] = self.api_calls_detail.get(key, 0) + n

    def increment_api_call(self, service: str) -> None:
        self.count(service)

    def usage_summary(self) -> dict:
        """Grouped tool-use totals for the report / coverage / model-compare surfaces."""
        srcs = {k: v for k, v in self.api_calls.items()
                if v and k not in self._TRANSPORT_KEYS and k not in self._LLM_KEYS}
        trans = {k: v for k, v in self.api_calls.items() if v and k in self._TRANSPORT_KEYS}
        llm = {k: v for k, v in self.api_calls.items() if v and k in self._LLM_KEYS}
        return {
            'sources': dict(sorted(srcs.items(), key=lambda x: -x[1])),
            'transport': dict(sorted(trans.items(), key=lambda x: -x[1])),
            'llm': llm,
            'detail': dict(sorted(self.api_calls_detail.items())),
            'cached': {k: v for k, v in self.api_calls_cache.items() if v},
            'total': sum(v for v in self.api_calls.values()),
        }

    def _progress(self, phase: str, message: str) -> None:
        if self.progress_callback:
            self.progress_callback({'phase': phase, 'message': message})

    @staticmethod
    def _random_country() -> str:
        return random.choice(PROXY_COUNTRIES)

    # ── html -> text ────────────────────────────────────────────────────────
    def html_to_text(self, html: str) -> str:
        html = re.sub(r'<(script|style|noscript)\b[^>]*>.*?</\1>', '', html, flags=re.I | re.S)
        html = re.sub(
            r'</?(?:div|p|br|hr|h[1-6]|li|tr|td|th|dt|dd|blockquote|section|article|'
            r'header|footer|nav|ul|ol|table|figcaption)\b[^>]*>', "\n", html, flags=re.I)
        text = re.sub(r'<[^>]+>', '', html)          # strip remaining tags
        text = _htmllib.unescape(text)
        text = re.sub(r'[ \t]+', ' ', text)
        lines = [ln.strip() for ln in text.split("\n")]
        return "\n".join(ln for ln in lines if ln)

    # ── simple GET (returns raw html or None) ───────────────────────────────
    def http_get(self, url: str, headers: dict | None = None, timeout: int = 20,
                 service: str | None = None) -> str | None:
        # `service` (e.g. 'sec', 'companies_house', 'opencorporates') counts this request
        # against that registry so the report's API-usage totals cover every source, not just
        # the ones (bizapedia/brightdata/acra) that were instrumented directly.
        if service:
            self.increment_api_call(service)
        try:
            r = requests.get(url, headers={'User-Agent': _UA, **(headers or {})},
                             timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and r.text:
                return r.text
        except requests.RequestException:
            pass
        return None

    # ── level 1: direct curl ────────────────────────────────────────────────
    def _http_fetch_text(self, url: str, meta: dict) -> str | None:
        self.count('http', op='direct')
        try:
            r = requests.get(url, headers={'User-Agent': _UA}, timeout=15, allow_redirects=True)
        except requests.RequestException:
            meta['http_code'] = 0
            return None
        meta['http_code'] = r.status_code
        if r.status_code != 200:
            return None
        return self.html_to_text(r.text)

    # ── level 2: Bright Data Web Unlocker ───────────────────────────────────
    def brightdata_fetch(self, url: str) -> str | None:
        api_key = self.config.get('brightdata_api_key') or ''
        zone = self.config.get('brightdata_zone') or 'web_unlocker1'
        if not api_key:
            return "Error: Bright Data not configured."
        self.count('brightdata')
        try:
            r = requests.post('https://api.brightdata.com/request',
                              headers={'Content-Type': 'application/json',
                                       'Authorization': f'Bearer {api_key}'},
                              json={'zone': zone, 'url': url, 'format': 'raw'},
                              timeout=240)
        except requests.RequestException as e:
            return f"Error: Bright Data returned HTTP 0 ({e})"
        if r.status_code != 200 or not r.text or len(r.text) < 200:
            return f"Error: Bright Data returned HTTP {r.status_code}"
        return self.html_to_text(r.text)

    def single_brightdata_fetch(self, url: str):
        """Returns (text_or_error, raw_html)."""
        api_key = self.config.get('brightdata_api_key') or ''
        zone = self.config.get('brightdata_zone') or 'web_unlocker1'
        if not api_key:
            return None, None
        self.count('brightdata')
        try:
            r = requests.post('https://api.brightdata.com/request',
                              headers={'Content-Type': 'application/json',
                                       'Authorization': f'Bearer {api_key}'},
                              json={'zone': zone, 'url': url, 'format': 'raw'}, timeout=240)
        except requests.RequestException as e:
            return f"Error: Bright Data returned HTTP 0 ({e})", None
        if r.status_code != 200 or not r.text or len(r.text) < 200:
            return f"Error: Bright Data returned HTTP {r.status_code}", None
        return self.html_to_text(r.text), r.text

    # ── level 3: Browserbase remote WebDriver ───────────────────────────────
    def browserbase_fetch_html(self, url: str) -> str | None:
        api_key = self.config.get('browserbase_api_key') or ''
        project_id = self.config.get('browserbase_project_id') or ''
        if not api_key or not project_id:
            return "Error: Browserbase not configured."
        self.count('browserbase')
        base = 'http://connect.usw2.browserbase.com/webdriver'
        try:
            sess = requests.post('https://api.browserbase.com/v1/sessions',
                                 headers={'x-bb-api-key': api_key, 'Content-Type': 'application/json'},
                                 json={'projectId': project_id}, timeout=30).json()
            bb_session_id = sess.get('id', '')
            if not bb_session_id:
                return "Error: Could not create Browserbase session."
            hdr = {'Content-Type': 'application/json', 'x-bb-api-key': api_key,
                   'session-id': bb_session_id}
            wd = requests.post(base + '/session', headers=hdr,
                               json={'capabilities': {'alwaysMatch': {'browserName': 'chrome'}}},
                               timeout=30).json()
            wd_session_id = (wd.get('value') or {}).get('sessionId', '')
            if not wd_session_id:
                return "Error: Could not create WebDriver session."
            requests.post(base + f'/session/{wd_session_id}/url', headers=hdr,
                          json={'url': url}, timeout=240)
            time.sleep(5)
            resp = requests.get(base + f'/session/{wd_session_id}/source',
                                headers={'x-bb-api-key': api_key, 'session-id': bb_session_id},
                                timeout=60).json()
            html = resp.get('value') or ''
            return html or "Error: Browserbase returned empty page."
        except requests.RequestException as e:
            return f"Error: Browserbase request failed ({e})"

    def single_browserbase_fetch(self, url: str):
        html = self.browserbase_fetch_html(url)
        if html is None or html.startswith('Error:'):
            return None, None
        text = self.html_to_text(html)
        if len(text) < 500 or _BLOCK_RE.search(text):
            return None, None
        return text, html

    # ── Bright Data Scraping Browser (CDP via Playwright) ───────────────────
    def single_scraping_browser_fetch(self, url: str):
        ws = self.config.get('brightdata_scraping_browser_ws') or ''
        if not ws:
            return "Error: Scraping Browser not configured.", None
        self.count('scraping_browser')
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(ws, timeout=120000)
                page = browser.new_page()
                page.goto(url, timeout=120000, wait_until='domcontentloaded')
                html = page.content()
                browser.close()
        except Exception as e:
            return f"Error: Scraping Browser failed ({e}).", None
        text = self.html_to_text(html).strip()
        if len(text) < 200:
            return "Error: Scraping Browser returned insufficient content.", None
        if _BLOCK_RE_SB.search(text) and len(text) < 1000:
            return "Error: Scraping Browser got a blocked/error page.", None
        return text, html

    # ── level 4: Wayback Machine ────────────────────────────────────────────
    def wayback_fetch(self, url: str) -> str | None:
        domain = re.sub(r'^https?://', '', url).split('/')[0] or url
        y = date.today().year
        for year in (str(y), str(y - 1), str(y - 2)):
            archive = f"https://web.archive.org/web/{year}id_/{url}"
            self.count('wayback')
            try:
                r = requests.get(archive, headers={'User-Agent': _UA}, timeout=20, allow_redirects=True)
            except requests.RequestException:
                continue
            if r.status_code == 200 and r.text and len(r.text) > 2000:
                text = self.html_to_text(r.text)
                if len(text) > 500 and not _BLOCK_RE.search(text):
                    return text
        return f"Error: No usable Wayback Machine snapshot found for {domain}."

    # ── the 4-level cascade ─────────────────────────────────────────────────
    def fetch_webpage(self, url: str):
        """Returns (text, meta). meta = {http_code, source, error}."""
        meta = {'http_code': 0, 'source': 'curl', 'error': None}

        self._progress('fetch', f"Trying direct curl for {url}...")
        text = self._http_fetch_text(url, meta)
        if text is not None and len(text) >= 200:
            self._progress('fetch', f"Direct curl succeeded — HTTP {meta['http_code']}, {len(text)} chars")
        else:
            if meta['http_code'] == 404:
                reason = 'HTTP 404'
            elif meta['http_code'] != 200:
                reason = f"HTTP {meta['http_code']}"
            else:
                reason = 'insufficient content'
            self._progress('fetch', f"Direct curl failed — {reason}")

        if (text is None or len(text) < 200) and meta['http_code'] != 404:
            self._progress('brightdata', "Trying Bright Data Web Unlocker...")
            meta['source'] = 'brightdata'
            text = self.brightdata_fetch(url)
            if (text or '').startswith('Error:'):
                self._progress('brightdata', f"Web Unlocker failed — {text}")
                meta['error'] = text
                text = None
            else:
                meta['http_code'] = 200
                self._progress('brightdata', f"Web Unlocker succeeded — {len(text)} chars")

        if (text is None or len(text) < 200) and meta['http_code'] != 404:
            self._progress('fetch', "Trying Browserbase remote browser...")
            meta['source'] = 'browserbase'
            html = self.browserbase_fetch_html(url)
            if html is None or html.startswith('Error:'):
                self._progress('fetch', f"Browserbase failed — {html}")
                meta['error'] = html
                text = None
            else:
                text = self.html_to_text(html)
            if text is not None and (len(text) < 500 or _BLOCK_RE.search(text)):
                self._progress('fetch', "Browserbase returned a blocked/error page — discarding")
                text = None
            if text is not None:
                meta['http_code'] = 200
                self._progress('fetch', f"Browserbase succeeded — {len(text)} chars")

        if (text is None or len(text) < 200) and meta['http_code'] != 404:
            self._progress('fetch', "Trying Wayback Machine...")
            meta['source'] = 'wayback'
            text = self.wayback_fetch(url)
            if (text or '').startswith('Error:'):
                self._progress('fetch', f"Wayback Machine failed — {text}")
                meta['error'] = text
                text = None
            else:
                meta['http_code'] = 200
                self._progress('fetch', f"Wayback Machine succeeded — {len(text)} chars")

        text = text or ''
        cap = self.config.get('max_page_chars', 8000)
        if len(text) > cap:
            text = text[:cap] + "\n... [truncated]"
        return text, meta

    # ── parallel curl for many urls ─────────────────────────────────────────
    def curl_fetch_multi(self, urls: list[str]) -> dict:
        self.count('http', op='multi', n=len(urls))
        def one(u):
            try:
                r = requests.get(u, headers={'User-Agent': _UA}, timeout=15, allow_redirects=True)
                code = r.status_code
                text = None
                html = None
                if code == 200 and r.text:
                    html = r.text
                    text = self.html_to_text(r.text)
                    if len(text) < 200:
                        text = None
                return u, {'text': text, 'http_code': code,
                           'html': html if code == 200 else None}
            except requests.RequestException:
                return u, {'text': None, 'http_code': 0, 'html': None}
        results = {}
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(urls)))) as ex:
            for u, res in ex.map(one, urls):
                results[u] = res
        return results

    # ── parallel browser fetches (PHP browserbaseFetchParallel / scrapingBrowserFetchParallel) ──
    def browserbase_fetch_parallel(self, urls: list[str]) -> dict:
        self.count('browserbase', op='parallel', n=len(urls))

        def one(u):
            text, html = self.single_browserbase_fetch(u)
            return (u, {'text': text, 'html': html}) if text else (u, None)
        out = {}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(urls)))) as ex:
            for u, res in ex.map(one, urls):
                if res:
                    out[u] = res
        return out

    def scraping_browser_fetch_parallel(self, urls: list[str]) -> dict:
        def one(u):
            text, html = self.single_scraping_browser_fetch(u)
            if text and not text.startswith('Error:'):
                return u, {'text': text, 'html': html}
            return u, None
        out = {}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(urls)))) as ex:
            for u, res in ex.map(one, urls):
                if res:
                    out[u] = res
        return out

    # ── WHOIS ───────────────────────────────────────────────────────────────
    def whois_lookup(self, domain: str) -> str:
        self.count('whois')
        try:
            out = subprocess.run(['whois', domain], capture_output=True, text=True,
                                 timeout=10).stdout
        except Exception:
            out = ''
        if not out:
            return "WHOIS data not available."
        keywords = ['registrant', 'creation', 'domain name', 'registrar', 'name server']
        lines = []
        for line in out.split("\n"):
            low = line.lower()
            if any(kw in low for kw in keywords):
                lines.append(line.strip())
        return "\n".join(lines[:30]) if lines else "WHOIS data not available or fully redacted."
