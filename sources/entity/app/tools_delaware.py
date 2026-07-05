"""
Entity Lookup v3b (Python) — Delaware Division of Corporations tool cluster.

Faithful port of the Delaware methods of php/tools.php (class LookupTools):
  - searchDelaware()               -> search_delaware()
  - lookupDelawareByFileNumber()   -> lookup_delaware_by_file_number()
  - wdPost() (private helper)       -> _wd_post()

The Delaware entity search (icis.corp.delaware.gov) sits behind bot protection and
requires a real browser + an ASP.NET form/postback flow, so — exactly like the PHP —
this drives a Browserbase remote browser over the Selenium **W3C WebDriver protocol**
(HTTP/JSON). That protocol is entirely HTTP-based, so it is reproduced faithfully with
`requests` (mirroring the PHP curl calls): create a Browserbase session, create a
WebDriver session, navigate, locate the input element, type into it, click submit, then
read `/source` and parse the results table with the identical regexes.

This class is combined with ToolBase (toolbase.py) via multiple inheritance. It calls
`self.config` for credentials; it does not otherwise depend on the base's fetch helpers
because the PHP Delaware methods issue their own raw curl calls to the WebDriver
endpoints (they do NOT go through browserbase_fetch_html / single_* helpers).

stdlib + requests only.
"""
from __future__ import annotations

import html as _htmllib
import json
import re
import time

import requests


class DelawareMixin:
    # ── logging (mirrors PHP `$this->log[] = [...]`) ─────────────────────────
    def _log_tool(self, tool: str, input, output) -> None:
        if not hasattr(self, 'log'):
            self.log = []
        self.log.append({'tool': tool, 'input': input, 'output': output})

    # ── WebDriver JSON POST helper (port of private LookupTools::wdPost) ──────
    def _wd_post(self, url: str, body, headers: dict) -> dict:
        # PHP: json_decode(curl_exec($ch), true) ?: []
        try:
            r = requests.post(url, headers=headers, data=json.dumps(body), timeout=30)
            parsed = r.json()
        except Exception:
            return {}
        return parsed if parsed else {}

    def _post_json(self, url: str, headers: dict, body, timeout: int = 30) -> dict:
        # Mirrors `json_decode(curl_exec($ch), true)` for the session-creation curls;
        # returns {} on any failure so downstream `.get(...)` yields '' (== PHP `?? ''`).
        try:
            r = requests.post(url, headers=headers, data=json.dumps(body), timeout=timeout)
            parsed = r.json()
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # ── searchDelaware ───────────────────────────────────────────────────────
    def search_delaware(self, entity_name: str) -> str:
        api_key = self.config.get('browserbase_api_key') or ''
        project_id = self.config.get('browserbase_project_id') or ''
        if not api_key or not project_id:
            result = "Delaware search unavailable (Browserbase not configured)."
            self._log_tool('search_delaware', entity_name, result)
            return result

        selenium_base = 'http://connect.usw2.browserbase.com/webdriver'

        # Create Browserbase session
        bb_session = self._post_json(
            'https://api.browserbase.com/v1/sessions',
            {'x-bb-api-key': api_key, 'Content-Type': 'application/json'},
            {'projectId': project_id},
            timeout=30,
        )
        bb_session_id = bb_session.get('id') or ''
        if not bb_session_id:
            result = "Delaware search unavailable (could not create browser session)."
            self._log_tool('search_delaware', entity_name, result)
            return result

        # Create WebDriver session
        wd = self._post_json(
            selenium_base + '/session',
            {'Content-Type': 'application/json', 'x-bb-api-key': api_key, 'session-id': bb_session_id},
            {'capabilities': {'alwaysMatch': {'browserName': 'chrome'}}},
            timeout=30,
        )
        wd_session_id = (wd.get('value') or {}).get('sessionId') or ''
        if not wd_session_id:
            result = "Delaware search unavailable (WebDriver failed)."
            self._log_tool('search_delaware', entity_name, result)
            return result

        headers = {'Content-Type': 'application/json', 'x-bb-api-key': api_key, 'session-id': bb_session_id}
        base = f"{selenium_base}/session/{wd_session_id}"

        # Navigate to Delaware entity search
        self._wd_post(f"{base}/url",
                      {'url': 'https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx'},
                      headers)
        time.sleep(3)

        # Find and fill the entity name input
        el = self._wd_post(f"{base}/element",
                           {'using': 'css selector', 'value': '#ctl00_ContentPlaceHolder1_frmEntityName'},
                           headers)
        el_vals = list((el.get('value') or {}).values())
        el_id = el_vals[0] if el_vals else ''
        if not el_id:
            result = "Delaware search failed (could not find search input)."
            self._log_tool('search_delaware', entity_name, result)
            return result

        self._wd_post(f"{base}/element/{el_id}/value", {'text': entity_name}, headers)
        time.sleep(1)

        # Click submit
        btn = self._wd_post(f"{base}/element",
                            {'using': 'css selector', 'value': '#ctl00_ContentPlaceHolder1_btnSubmit'},
                            headers)
        btn_vals = list((btn.get('value') or {}).values())
        btn_id = btn_vals[0] if btn_vals else ''
        if btn_id:
            self._wd_post(f"{base}/element/{btn_id}/click", {}, headers)
        time.sleep(5)

        # Get results page
        html = ''
        try:
            r = requests.get(f"{base}/source",
                             headers={'x-bb-api-key': api_key, 'session-id': bb_session_id},
                             timeout=30)
            source = r.json()
        except Exception:
            source = {}
        html = (source or {}).get('value') or ''

        # Parse results: each row has a file number span and entity name link
        results = []
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.I | re.S):
            link_m = re.search(r'<a[^>]*>([^<]+)</a>', row, re.I)
            if not link_m:
                continue
            name = _htmllib.unescape(link_m.group(1)).strip()
            if len(name) < 3:
                continue
            # Extract file number
            file_num = ''
            fn_m = re.search(r'<span[^>]*lblFileNumber[^>]*>(\d+)</span>', row)
            if fn_m:
                file_num = fn_m.group(1)
            if not file_num:
                continue  # skip non-result rows
            results.append(f"File #{file_num}: {name}")
            if len(results) >= 15:
                break

        result = "\n".join(results) if results else f"No Delaware entities found for \"{entity_name}\"."
        self._log_tool('search_delaware', entity_name, result)
        return result

    # ── lookupDelawareByFileNumber ───────────────────────────────────────────
    def lookup_delaware_by_file_number(self, file_number: str):
        """
        Look up a Delaware entity by file number for validation.
        Searches by file number and parses the results list (detail page requires
        JS postback which doesn't work via WebDriver).
        Returns {'name': ..., 'file_number': ..., 'status': ...} or None.
        """
        api_key = self.config.get('browserbase_api_key') or ''
        project_id = self.config.get('browserbase_project_id') or ''
        if not api_key or not project_id:
            self._log_tool('lookup_delaware', file_number, 'Browserbase not configured')
            return None

        selenium_base = 'http://connect.usw2.browserbase.com/webdriver'

        # Create Browserbase session
        bb_session = self._post_json(
            'https://api.browserbase.com/v1/sessions',
            {'x-bb-api-key': api_key, 'Content-Type': 'application/json'},
            {'projectId': project_id},
            timeout=30,
        )
        bb_session_id = bb_session.get('id') or ''
        if not bb_session_id:
            self._log_tool('lookup_delaware', file_number, 'Could not create browser session')
            return None

        # Create WebDriver session
        wd = self._post_json(
            selenium_base + '/session',
            {'Content-Type': 'application/json', 'x-bb-api-key': api_key, 'session-id': bb_session_id},
            {'capabilities': {'alwaysMatch': {'browserName': 'chrome'}}},
            timeout=30,
        )
        wd_session_id = (wd.get('value') or {}).get('sessionId') or ''
        if not wd_session_id:
            self._log_tool('lookup_delaware', file_number, 'WebDriver failed')
            return None

        headers = {'Content-Type': 'application/json', 'x-bb-api-key': api_key, 'session-id': bb_session_id}
        base = f"{selenium_base}/session/{wd_session_id}"

        # Navigate to Delaware entity search
        self._wd_post(f"{base}/url",
                      {'url': 'https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx'},
                      headers)
        time.sleep(3)

        # Fill in the file number field
        el = self._wd_post(f"{base}/element",
                           {'using': 'css selector', 'value': '#ctl00_ContentPlaceHolder1_frmFileNumber'},
                           headers)
        el_vals = list((el.get('value') or {}).values())
        el_id = el_vals[0] if el_vals else ''
        if not el_id:
            self._log_tool('lookup_delaware', file_number, 'Could not find file number input')
            return None

        self._wd_post(f"{base}/element/{el_id}/value", {'text': file_number}, headers)
        time.sleep(1)

        # Click submit
        btn = self._wd_post(f"{base}/element",
                            {'using': 'css selector', 'value': '#ctl00_ContentPlaceHolder1_btnSubmit'},
                            headers)
        btn_vals = list((btn.get('value') or {}).values())
        btn_id = btn_vals[0] if btn_vals else ''
        if btn_id:
            self._wd_post(f"{base}/element/{btn_id}/click", {}, headers)
        time.sleep(5)

        # Get the page source
        try:
            r = requests.get(f"{base}/source",
                             headers={'x-bb-api-key': api_key, 'session-id': bb_session_id},
                             timeout=30)
            source = r.json()
        except Exception:
            source = {}
        html = (source or {}).get('value') or ''

        # Parse results list — file number search returns same format as name search
        result = None
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.I | re.S):
            fn = ''
            fn_m = re.search(r'<span[^>]*lblFileNumber[^>]*>(\d+)</span>', row)
            if fn_m:
                fn = fn_m.group(1)
            if fn != file_number:
                continue

            link_m = re.search(r'<a[^>]*>([^<]+)</a>', row, re.I)
            if link_m:
                name = _htmllib.unescape(link_m.group(1)).strip()
                result = {
                    'name': name,
                    'file_number': file_number,
                    'status': 'Active',  # entity appears in search results = exists in registry
                }
                break

        self._log_tool('lookup_delaware', file_number,
                       json.dumps(result, separators=(',', ':')) if result else 'No entity found')
        return result
