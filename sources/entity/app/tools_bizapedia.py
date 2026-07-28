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

    def search_bizapedia(self, entity_name: str, state: str = "") -> list:
        """
        Search Bizapedia for a US entity name. Returns list of company records.
        Each record has: EntityName, FileNumber, FilingJurisdictionName,
        FilingStatus, EntityType, FilingDate, principal address, registered agent,
        principals/officers, etc.

        `state` (optional postal abbreviation) narrows via Bizapedia's `pa` param — used
        by the branch-triangulation sweep to recover a parent in its home jurisdiction.
        """
        self.api_calls['bizapedia'] += 1
        self._progress('registry', f'Searching Bizapedia for "{entity_name}"...'
                       + (f' (state={state})' if state else ''))

        params = {
            'ep': 'LCSBN',
            'k': self.BIZAPEDIA_API_KEY,
            'n': entity_name,
        }
        if state:
            params['pa'] = state

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

    # ── Branch triangulation ──────────────────────────────────────────────
    # A foreign/branch registration names both the entity AND its home jurisdiction.
    # Multiple branches pointing to the same home (esp. with shared officers) is a
    # high-precision signal for the real parent — and lets us recover a parent the
    # capped name-search never returned. See build_bizapedia_families().
    @staticmethod
    def _biz_is_foreign(r):
        et = (r.get('EntityType') or r.get('type') or '').upper()
        return 'FOREIGN' in et or 'OUT OF STATE' in et or 'NON-LOUISIANA' in et

    def build_bizapedia_families(self, name):
        """Search Bizapedia, then actively detect BRANCH (foreign) registrations, sweep the
        home jurisdictions they point to (to recover the parent + siblings the 50-cap hid),
        and fuse each parent+branches into a triangulation block.

        Returns (family_block_or_None, all_deduped_records). Extra state sweeps run ONLY when
        branches are actually detected, so non-branch entities cost exactly one search."""
        import collections
        import re

        base = self.search_bizapedia(name)
        if not base:
            return None, []

        all_recs = list(base)
        homes = []
        for r in base:
            if self._biz_is_foreign(r):
                h = (r.get('DomesticJurisdictionPostalAbbreviation') or '').strip().upper()
                if h and h not in homes:
                    homes.append(h)
        for hs in homes[:3]:                       # cap sweeps to bound cost
            try:
                all_recs += self.search_bizapedia(name, state=hs)
            except Exception:                       # noqa: BLE001
                pass

        seen = {}
        for r in all_recs:
            key = (r.get('FilingJurisdictionPostalAbbreviation') or '', r.get('FileNumber') or '')
            seen.setdefault(key, r)
        recs = list(seen.values())

        def norm(s):
            return re.sub(r'[^A-Z0-9 ]', '', (s or '').upper()).strip()

        fams = collections.defaultdict(list)
        for r in recs:
            fams[norm(r.get('EntityName'))].append(r)

        scored = []
        for fam_recs in fams.values():
            branches = [r for r in fam_recs
                        if self._biz_is_foreign(r)
                        and (r.get('DomesticJurisdictionPostalAbbreviation') or '').strip()]
            if not branches:
                continue
            scored.append((len(branches), self._format_bizapedia_family(fam_recs, branches)))
        if not scored:
            return None, recs
        scored.sort(key=lambda x: -x[0])
        block = "\n\n".join(b for _, b in scored[:2])   # top 2 families by branch count
        return block, recs

    def _format_bizapedia_family(self, fam_recs, branches):
        """Fuse a parent+branches cluster into a compact, prompt-ready triangulation block."""
        import collections

        def addr(r):
            a = [r.get('PrincipalAddressLine1') or r.get('MailingAddressLine1'),
                 r.get('PrincipalAddressCity') or r.get('MailingAddressCity'),
                 r.get('PrincipalAddressState') or r.get('MailingAddressState')]
            return ', '.join(x for x in a if x)

        name = (fam_recs[0].get('EntityName') or '').strip()
        home = collections.Counter(
            (b.get('DomesticJurisdictionPostalAbbreviation') or '').upper() for b in branches
        ).most_common(1)[0][0]
        branch_states = sorted({(b.get('FilingJurisdictionPostalAbbreviation') or '').upper() for b in branches})
        states = {(r.get('FilingJurisdictionPostalAbbreviation') or '') for r in fam_recs}
        hq = collections.Counter(addr(r) for r in branches if addr(r))
        home_recs = [r for r in fam_recs if not self._biz_is_foreign(r)]

        off_files = collections.defaultdict(set)
        off_titles = collections.defaultdict(set)
        for r in fam_recs:
            st = (r.get('FilingJurisdictionPostalAbbreviation') or '').upper()
            for p in (r.get('Principals') or []):
                nm = p.get('PrincipalName')
                if not nm:
                    continue
                off_files[nm].add(st)
                if p.get('Titles'):
                    off_titles[nm].add(p['Titles'])

        lines = [f'ENTITY FAMILY "{name}" — {len(fam_recs)} filings across {len(states)} states',
                 f'  Branches: {len(branch_states)} ({", ".join(branch_states)}) all point HOME -> {home}']
        if home_recs:
            lines.append(f'  Home/domestic filing present in {home} (file# {home_recs[0].get("FileNumber")}) '
                         f'— recommend this HOME entity as the contracting party')
        else:
            lines.append(f'  Home/domestic filing: {home} (not returned by search — the parent is in {home})')
        if hq:
            lines.append(f'  Operating HQ (from branch filings): {hq.most_common(1)[0][0]}')
        corr = [(nm, f) for nm, f in off_files.items() if len(f) > 1]
        if corr:
            lines.append('  Officers on >1 filing (corroborated core team — identity lock vs same-name entities):')
            for nm, f in sorted(corr, key=lambda x: -len(x[1])):
                lines.append(f'    * {nm} [{",".join(sorted(f))}] {"; ".join(sorted(off_titles[nm]))[:50]}')
        lines.append('  Filings:')
        for r in sorted(fam_recs, key=lambda r: (self._biz_is_foreign(r), r.get('FilingJurisdictionPostalAbbreviation') or '')):
            role = 'branch' if self._biz_is_foreign(r) else 'HOME'
            fd = ((r.get('FilingDate') or {}).get('Date') or '')[:10]
            extra = (f' -> home {(r.get("DomesticJurisdictionPostalAbbreviation") or "")}'
                     if self._biz_is_foreign(r) else '')
            lines.append(f'    [{role}] {r.get("FilingJurisdictionPostalAbbreviation")} '
                         f'#{r.get("FileNumber")} {(r.get("EntityType") or "")[:34]} '
                         f'({r.get("FilingStatus")}, {fd}){extra}')
        return "\n".join(lines)


def _json_decode(response: str | None):
    """Mirror PHP json_decode($response, true): returns parsed value or None."""
    if not response:
        return None
    try:
        return json.loads(response)
    except (ValueError, TypeError):
        return None
