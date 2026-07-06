"""
Entity Lookup v3b (Python) — Bizapedia tool cluster.

Faithful like-for-like port of the Bizapedia methods from php/tools.php (class
LookupTools):

    lookupBizapediaByFileNumber -> lookup_bizapedia_by_file_number
    searchBizapedia             -> search_bizapedia
    searchBizapediaTrademark    -> search_bizapedia_trademark

Bizapedia is queried through its REST endpoint bizapedia.com/bdmservice-rest.aspx.
This mixin is combined with ToolBase (and the other tool clusters) via multiple
inheritance in tools.py. It calls foundation helpers on ``self`` — it does not
reimplement them:

    self.api_calls['bizapedia']   incremented exactly as the PHP does
    self._progress(phase, message)
    self.log                      appended to exactly as $this->log[] is

The PHP uses raw curl (no User-Agent, per-call timeouts, CURLOPT_ENCODING => '').
We reproduce that request signature with the sync ``requests`` library rather than
routing through self.http_get, because http_get injects a Mozilla User-Agent that
the PHP curl never sends and it discards the HTTP status code these methods need.
"""
from __future__ import annotations

import json

import requests

BIZAPEDIA_REST_URL = 'https://www.bizapedia.com/bdmservice-rest.aspx'


class BizapediaMixin:
    # private const BIZAPEDIA_API_KEY = 'YBUIWJDRQYMBKXCQDA';
    BIZAPEDIA_API_KEY = 'YBUIWJDRQYMBKXCQDA'

    # ── Bizapedia ────────────────────────────────────────────────────────────

    def lookup_bizapedia_by_file_number(self, file_number: str, state_code: str) -> dict | None:
        """
        Look up a specific entity on Bizapedia by file number and state.
        Returns the raw company record or None if not found.
        """
        self.api_calls['bizapedia'] += 1
        params = {
            'ep': 'LCBFN',
            'k': self.BIZAPEDIA_API_KEY,
            'fn': file_number,
            'pa': state_code.upper(),
        }

        # curl: RETURNTRANSFER, TIMEOUT 15, ENCODING '' (accept any encoding)
        try:
            r = requests.get(BIZAPEDIA_REST_URL, params=params, timeout=15)
            http_code = r.status_code
            response = r.text
        except requests.RequestException:
            http_code = 0
            response = None

        if http_code != 200 or not response:
            return None

        data = _json_decode(response)
        if not data or not (data.get('Success') or False) or not data.get('EntityName'):
            return None

        return data

    def search_bizapedia(self, entity_name: str) -> list:
        """
        Search Bizapedia for a US entity name. Returns list of company records.
        Each record has: EntityName, FileNumber, FilingJurisdictionName,
        FilingStatus, EntityType, FilingDate, principal address, registered agent,
        principals/officers, etc.
        """
        self.api_calls['bizapedia'] += 1
        self._progress('registry', f'Searching Bizapedia for "{entity_name}"...')

        params = {
            'ep': 'LCSBN',
            'k': self.BIZAPEDIA_API_KEY,
            'n': entity_name,
        }

        # curl: RETURNTRANSFER, TIMEOUT 30, ENCODING ''
        try:
            r = requests.get(BIZAPEDIA_REST_URL, params=params, timeout=30)
            http_code = r.status_code
            response = r.text
        except requests.RequestException:
            http_code = 0
            response = None

        if http_code != 200 or not response:
            self.log.append({'tool': 'bizapedia', 'input': entity_name, 'output': f"HTTP {http_code}"})
            return []

        data = _json_decode(response)
        if not data or not data.get('Success'):
            # PHP: 'API error: ' . ($data['ErrorMessage'] ?? 'unknown')
            err = (data.get('ErrorMessage') if data else None)
            err = err if err is not None else 'unknown'
            self.log.append({
                'tool': 'bizapedia',
                'input': entity_name,
                'output': 'API error: ' + err,
            })
            return []

        companies = data.get('Companies') or []
        self.log.append({'tool': 'bizapedia', 'input': entity_name, 'output': f"{len(companies)} results"})
        self._progress('registry', f'Bizapedia: {len(companies)} results for "{entity_name}"')
        return companies

    def search_bizapedia_trademark(self, owner_name: str) -> str:
        """
        Search Bizapedia trademarks by owner name. Returns a formatted string
        summarising trademarks owned by the given entity.
        """
        self.api_calls['bizapedia'] += 1
        self._progress('registry', f'Searching Bizapedia trademarks for owner "{owner_name}"...')

        params = {
            'ep': 'LT',
            'k': self.BIZAPEDIA_API_KEY,
            'tm': '',
            'tmo': owner_name,
        }

        # curl: RETURNTRANSFER, TIMEOUT 30, ENCODING ''
        try:
            r = requests.get(BIZAPEDIA_REST_URL, params=params, timeout=30)
            http_code = r.status_code
            response = r.text
        except requests.RequestException:
            http_code = 0
            response = None

        if http_code != 200 or not response:
            result = f"No trademark results (HTTP {http_code})."
            self.log.append({'tool': 'bizapedia_tm', 'input': owner_name, 'output': result})
            return result

        data = _json_decode(response)
        if not data or not data.get('Success') or not data.get('Trademarks'):
            result = f'No trademarks found for owner "{owner_name}".'
            self.log.append({'tool': 'bizapedia_tm', 'input': owner_name, 'output': result})
            return result

        trademarks = data['Trademarks']
        self._progress('registry', f'Bizapedia TM: {len(trademarks)} trademarks for "{owner_name}"')

        # Build compact summary grouped by owner
        by_owner: dict = {}
        for t in trademarks:
            owner = t.get('OwnerName') or 'Unknown'
            by_owner.setdefault(owner, []).append(t)

        lines = []
        for owner, marks in by_owner.items():
            active = [t for t in marks if 'registered' in (t.get('StatusDescription') or '').lower()]
            lines.append(f"{owner} — {len(marks)} trademarks ({len(active)} active)")

            # Show owner address from first mark that has one
            for t in marks:
                addr = [x for x in [
                    t.get('OwnerAddressLine1') or '', t.get('OwnerAddressLine2') or '',
                    t.get('OwnerAddressCity') or '', t.get('OwnerAddressState') or '',
                ] if x]
                if addr:
                    lines.append("  Address: " + ", ".join(addr))
                    break

            # State of incorporation
            if marks[0].get('OwnerNationalityStateName') or '':
                lines.append("  State: " + marks[0]['OwnerNationalityStateName'])

            # List active marks (up to 10)
            active_marks = active[:10]
            for t in active_marks:
                filed = (t.get('FilingDate') or {})
                filed = (filed.get('Date') if isinstance(filed, dict) else None) or ''
                lines.append(
                    f"  TM: {t.get('MarkIdentification', '')} "
                    f"(Reg #{t.get('RegistrationNumber', '')}, filed {filed[:10]})"
                )
            if len(active) > 10:
                lines.append(f"  ... and {len(active) - 10} more active trademarks")

        result = "\n".join(lines)
        self.log.append({'tool': 'bizapedia_tm', 'input': owner_name, 'output': result})
        return result

    def search_trademarks(self, query: str, mode: str = 'name') -> dict:
        """Bizapedia trademark search by mark name (mode='name') or owner (mode='owner').
        Faithful to bizapedia_tm.php; returns {'results': [...], 'error': str|None}."""
        self.api_calls['bizapedia'] += 1
        params = {'ep': 'LT', 'k': self.BIZAPEDIA_API_KEY}
        if mode == 'owner':
            params['tm'] = ''
            params['tmo'] = query
        else:
            params['tm'] = query
            params['tmo'] = ''
        try:
            r = requests.get(BIZAPEDIA_REST_URL, params=params, timeout=30)
            http_code = r.status_code
            response = r.text
        except requests.RequestException:
            http_code = 0
            response = None
        if http_code != 200 or not response:
            return {'results': [], 'error': f"HTTP {http_code} — no response from Bizapedia API."}
        data = _json_decode(response)
        if not data or not data.get('Success'):
            return {'results': [], 'error': 'API error: ' + ((data.get('ErrorMessage') if data else None) or 'unknown')}
        results = data.get('Trademarks') or []
        if not results:
            return {'results': [], 'error': f'No trademarks found for "{query}".'}
        return {'results': results, 'error': None}

    # ── result ranking/dedup helpers (PHP static methods, ~L3508-3623) ──────
    def bizapedia_type_rank(self, type_str):
        upper = (type_str or '').upper()
        if 'FICTITIOUS' in upper:
            return 2
        if 'FOREIGN' in upper or 'OUT OF STATE' in upper:
            return 1
        return 0  # domestic / normal entity

    def sort_bizapedia_results(self, results):
        """PHP sortBizapediaResults(array &$results): void — sorts IN PLACE."""
        results.sort(key=lambda r: (
            0 if ((r.get('FilingStatus') or r.get('status') or '').lower()) in ('active', 'unknown') else 1,
            self.bizapedia_type_rank(r.get('EntityType') or r.get('type') or ''),
            0 if (r.get('DomesticJurisdiction') or r.get('domestic_jurisdiction') or '').lower()
                 == (r.get('Jurisdiction') or r.get('jurisdiction') or '').lower() else 1,
        ))

    def deduplicate_bizapedia_results(self, all_results):
        """PHP deduplicateBizapediaResults(array): string — compact records + sort, JSON out."""
        seen = set()
        unique = []
        for r in all_results:
            key = (r.get('FilingJurisdictionPostalAbbreviation') or '') + ':' + (r.get('FileNumber') or '')
            if key in seen:
                continue
            seen.add(key)
            record = {
                'name': r.get('EntityName') or '',
                'status': r.get('FilingStatus') or 'Unknown',
                'type': r.get('EntityType') or '',
                'jurisdiction': r.get('FilingJurisdictionName') or '',
                'jurisdiction_code': r.get('FilingJurisdictionPostalAbbreviation') or '',
                'file_number': r.get('FileNumber') or '',
                'filing_date': (((r.get('FilingDate') or {}).get('Date') or '')[:10]) or None,
                'domestic_jurisdiction': r.get('DomesticJurisdictionName') or '',
            }
            addr = [x for x in [r.get('PrincipalAddressLine1') or '', r.get('PrincipalAddressLine2') or '',
                                r.get('PrincipalAddressCity') or '', r.get('PrincipalAddressState') or '',
                                r.get('PrincipalAddressPostalCode') or ''] if x]
            if addr:
                record['address'] = ', '.join(addr)
            if r.get('RegisteredAgentName'):
                record['registered_agent'] = r['RegisteredAgentName']
            akas = [x for x in [r.get('OtherEntityName1') or '', r.get('OtherEntityName2') or '',
                                r.get('OtherEntityName3') or ''] if x]
            if akas:
                record['alternative_names'] = akas
            principals = []
            for p in (r.get('Principals') or []):
                entry = p.get('PrincipalName') or ''
                if p.get('Titles'):
                    entry += ' (' + p['Titles'] + ')'
                if entry:
                    principals.append(entry)
            if principals:
                record['principals'] = principals
            if r.get('PrimaryDomainName'):
                record['website'] = r['PrimaryDomainName']
            if r.get('PrimaryEmail'):
                record['email'] = r['PrimaryEmail']
            if r.get('PrimaryPhone'):
                record['phone'] = r['PrimaryPhone']
            if r.get('BusinessDescription'):
                record['description'] = r['BusinessDescription']
            unique.append(record)

        if not unique:
            return 'No Bizapedia results found.'
        unique.sort(key=lambda a: (
            0 if (a.get('status') or '').lower() in ('active', 'unknown') else 1,
            self.bizapedia_type_rank(a.get('type') or ''),
            0 if (a.get('domestic_jurisdiction') or '').lower() == (a.get('jurisdiction') or '').lower() else 1,
        ))
        return json.dumps(unique, indent=4, ensure_ascii=False)


def _json_decode(response: str | None):
    """Mirror PHP json_decode($response, true): returns parsed value or None."""
    if not response:
        return None
    try:
        return json.loads(response)
    except (ValueError, TypeError):
        return None
