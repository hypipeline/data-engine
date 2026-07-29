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

    def search_bizapedia(self, entity_name: str, state: str = "", quiet: bool = False) -> list:
        """
        Search Bizapedia for a US entity name. Returns list of company records.
        Each record has: EntityName, FileNumber, FilingJurisdictionName,
        FilingStatus, EntityType, FilingDate, principal address, registered agent,
        principals/officers, etc.

        `state` (optional postal abbreviation) narrows via Bizapedia's `pa` param — used to
        recover the domestic parent in a branch's home jurisdiction. `quiet` suppresses the
        per-call progress line.

        Robustness: results are memoised per (name, state) for the life of this instance, and a
        transient HTTP error / throttle is RETRIED with backoff rather than being silently treated
        as "0 results" — that false-zero is what previously lost the herculite→Aberdeen linkage.
        """
        import time

        memo = self.__dict__.setdefault('_biz_memo', {})
        mkey = ((entity_name or '').strip().lower(), (state or '').strip().upper())
        if mkey in memo:
            return memo[mkey]

        # Fix D backstop: no single lookup should ever fire a runaway number of Bizapedia calls
        # (the sweep bug reached 109-160). Normal lookups use ~12; this cap is pure insurance and
        # logs loudly if hit so the cause gets investigated rather than silently starving searches.
        budget = self.__dict__.get('_biz_budget', 60)
        if self.api_calls.get('bizapedia', 0) >= budget:
            self._progress('registry', f'⚠ Bizapedia call budget ({budget}) reached — skipping '
                           f'"{entity_name}"' + (f' ({state})' if state else '')
                           + ' (backstop hit; investigate call volume)')
            memo[mkey] = []
            return []

        if not quiet:
            self._progress('registry', f'Searching Bizapedia for "{entity_name}"...'
                           + (f' (state={state})' if state else ''))

        params = {'ep': 'LCSBN', 'k': self.BIZAPEDIA_API_KEY, 'n': entity_name}
        if state:
            params['pa'] = state

        data = None
        for attempt in range(3):                       # 1 try + 2 retries on transient failure
            self.api_calls['bizapedia'] += 1
            try:
                r = requests.get(BIZAPEDIA_REST_URL, params=params, timeout=30)
                http_code, response = r.status_code, r.text
            except requests.RequestException:
                http_code, response = 0, None

            if http_code == 200 and response:
                data = _json_decode(response)
                if data and data.get('Success'):
                    break                              # genuine success (Companies may be [])
            # transient failure (non-200, empty body, or API error) → back off and retry
            if attempt < 2:
                self._progress('registry', f'Bizapedia throttled/error for "{entity_name}"'
                               + (f' ({state})' if state else '') + f' — retry {attempt + 1}/2')
                time.sleep(0.6 * (attempt + 1))
                data = None

        if not data or not data.get('Success'):
            # Exhausted retries: this is an ERROR/throttle, NOT a confirmed empty result.
            self.log.append({'tool': 'bizapedia', 'input': entity_name, 'output': 'API error/throttled after retries'})
            self._progress('registry', f'Bizapedia: NO RESPONSE for "{entity_name}" (throttled/error — not a real 0)')
            memo[mkey] = []
            return []

        companies = data.get('Companies') or []
        self.log.append({'tool': 'bizapedia', 'input': entity_name, 'output': f"{len(companies)} results"})
        self._progress('registry', f'Bizapedia: {len(companies)} results for "{entity_name}"')
        memo[mkey] = companies
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

    @staticmethod
    def bizapedia_owner_hints(records):
        """Detect trade-name / DBA / fictitious-name records that name an OWNER. Returns
        (hint_text, owner_names). The linkage is the answer for cases like herculite.com: the
        site's 'Herculite Products, Inc.' is a DBA of ABERDEEN ROAD COMPANY.

        hint_text is prepended to the evidence so it survives truncation (the raw records that
        carry it rank last and get cut). owner_names lets the caller SEARCH the owner so its own
        filing — with the registry_id — reaches the analysis (else the owner resolves with no id)."""
        import re
        hints, owners, seen = [], [], set()
        for r in records or []:
            name = (r.get('EntityName') or r.get('name') or '').strip()
            etype = (r.get('EntityType') or r.get('type') or '')
            owner = trade = None
            m = re.search(r'(.+?)\s+DBA\s+(.+)', name, re.I)                 # "OWNER DBA TRADE"
            pm = re.search(r'^(.*?)\s*\(([^)]+)\)\s*$', name)               # "TRADE (OWNER)"
            if m:
                owner, trade = m.group(1).strip(), m.group(2).strip()
            elif pm and any(w in pm.group(2).upper().split()
                            for w in ['COMPANY', 'CORP', 'CORPORATION', 'INC', 'LLC', 'LP', 'LTD', 'CO']):
                trade, owner = pm.group(1).strip(), pm.group(2).strip()
            elif 'FICTITIOUS' in etype.upper() or 'TRADE NAME' in etype.upper():
                for p in (r.get('Principals') or []):
                    if 'owner' in (p.get('Titles') or '').lower() and p.get('PrincipalName'):
                        owner, trade = p['PrincipalName'].strip(), name
                        break
            if owner and owner.upper() not in seen and owner.upper() != (trade or '').upper():
                seen.add(owner.upper())
                owners.append(owner)
                hints.append(f'  - "{trade}" is a trade/DBA name; its owning legal entity is '
                             f'{owner} (recommend {owner}, not the trade name)')
        if not hints:
            return '', []
        text = ("TRADE NAME / OWNER LINKAGES (recommend the OWNER legal entity, not the trade name):\n"
                + "\n".join(hints))
        return text, owners

    # ── Branch triangulation ──────────────────────────────────────────────
    # A foreign/branch registration names both the entity AND its home jurisdiction.
    # Multiple branches pointing to the same home (esp. with shared officers) is a
    # high-precision signal for the real parent — and lets us recover a parent the
    # capped name-search never returned. See build_bizapedia_families().
    @staticmethod
    def _biz_is_foreign(r):
        et = (r.get('EntityType') or r.get('type') or '').upper()
        return 'FOREIGN' in et or 'OUT OF STATE' in et or 'NON-LOUISIANA' in et

    # Canonicalise legal-suffix synonyms so 'Inc'≡'Incorporated', 'Corp'≡'Corporation', etc.
    # cluster together — but DIFFERENT types (Inc vs LLC) map to different tokens and stay
    # distinct. Multi-word forms must precede the bare 'LIMITED' so they win first.
    _SUFFIX_SYN = [("LIMITED LIABILITY COMPANY", "LLC"), ("LIMITED LIABILITY PARTNERSHIP", "LLP"),
                   ("LIMITED PARTNERSHIP", "LP"), ("INCORPORATED", "INC"), ("CORPORATION", "CORP"),
                   ("COMPANY", "CO"), ("LIMITED", "LTD")]

    @classmethod
    def _biz_norm_name(cls, s):
        """Normalised entity name for clustering: strip punctuation, collapse spaces, and
        canonicalise legal-suffix synonyms (Inc/Incorporated, Corp/Corporation, ...)."""
        import re
        s = re.sub(r"[^A-Z0-9 ]", "", (s or "").upper())
        s = re.sub(r"\s+", " ", s).strip()
        for lng, sht in cls._SUFFIX_SYN:
            s = re.sub(r"\b" + lng + r"\b", sht, s)
        return s.strip()

    @staticmethod
    def _biz_home_of(r):
        """The home jurisdiction of a record: the domestic_jurisdiction a branch points to, or
        (for a domestic filing) its own filing state — a domestic record IS its own home."""
        if BizapediaMixin._biz_is_foreign(r):
            return (r.get('DomesticJurisdictionPostalAbbreviation') or '').strip().upper()
        return (r.get('FilingJurisdictionPostalAbbreviation') or '').strip().upper()

    def build_bizapedia_families(self, name):
        """Detect branch structure from a SINGLE unfiltered name search — the base search already
        returns up to 50 records spanning all states, branches included, each carrying its own
        home jurisdiction. Branch detection is post-processing on that one result set.

        The only follow-up calls are TARGETED: when branches point to a home jurisdiction whose
        domestic PARENT record isn't already in the results, fetch it with one `pa=<home>` call per
        distinct home (capped). No per-state sweep — that previously fired ~50 calls/name, blew the
        Bizapedia rate limit, and starved later searches (which then returned false zeros).

        Families are keyed on (normalised name, HOME jurisdiction) so different same-name companies
        don't merge, and the recommended parent is the domestic filing whose OWN state is the home.

        Returns (family_block_or_None, all_deduped_records)."""
        import collections

        base = self.search_bizapedia(name)
        if not base:
            return None, []
        # Branches with a REAL home (populated DomesticJurisdiction) are the only ones we can
        # triangulate. A "Foreign" type with a blank home gives nothing to recover — skip it.
        homes = []
        for r in base:
            if self._biz_is_foreign(r):
                h = (r.get('DomesticJurisdictionPostalAbbreviation') or '').strip().upper()
                if h and h not in homes:
                    homes.append(h)
        if not homes:
            return None, base                          # no triangulatable branch structure — 1 call

        # Targeted parent recovery: only fetch a home state if its domestic parent isn't already
        # present in the base results. Capped at 3 distinct homes to bound calls.
        all_recs = list(base)
        have_home = {(self._biz_norm_name(r.get('EntityName')),
                      (r.get('FilingJurisdictionPostalAbbreviation') or '').strip().upper())
                     for r in base if not self._biz_is_foreign(r)}
        base_names = {self._biz_norm_name(r.get('EntityName')) for r in base if self._biz_is_foreign(r)}
        for home in homes[:3]:
            if any((nm, home) in have_home for nm in base_names):
                continue                               # parent already in results — no call needed
            self._progress('registry', f'Recovering home-jurisdiction parent for "{name}" in {home}...')
            try:
                all_recs += self.search_bizapedia(name, state=home, quiet=True) or []
            except Exception:                          # noqa: BLE001
                pass

        seen = {}
        for r in all_recs:
            key = (r.get('FilingJurisdictionPostalAbbreviation') or '', r.get('FileNumber') or '')
            seen.setdefault(key, r)
        recs = list(seen.values())

        fams = collections.defaultdict(list)
        for r in recs:
            home = self._biz_home_of(r)
            if home:
                fams[(self._biz_norm_name(r.get('EntityName')), home)].append(r)

        scored = []
        for (fam_name, home), fam_recs in fams.items():
            branches = [r for r in fam_recs if self._biz_is_foreign(r)]
            if not branches:
                continue
            scored.append((len(branches), self._format_bizapedia_family(fam_recs, branches, home)))
        if not scored:
            return None, recs
        scored.sort(key=lambda x: -x[0])
        block = "\n\n".join(b for _, b in scored[:2])   # top 2 families by branch count
        return block, recs

    def _format_bizapedia_family(self, fam_recs, branches, home):
        """Compact, prompt-frugal triangulation block — summarise (branch count + states, the
        home/parent filing, officer overlap, HQ) rather than listing every filing. The LLM word
        budget matters even though Bizapedia ingest does not."""
        import collections

        def addr(r):
            a = [r.get('PrincipalAddressLine1') or r.get('MailingAddressLine1'),
                 r.get('PrincipalAddressCity') or r.get('MailingAddressCity'),
                 r.get('PrincipalAddressState') or r.get('MailingAddressState')]
            return ', '.join(x for x in a if x)

        name = (fam_recs[0].get('EntityName') or '').strip()
        branch_states = sorted({(b.get('FilingJurisdictionPostalAbbreviation') or '').upper() for b in branches})
        hq = collections.Counter(addr(r) for r in branches if addr(r))
        # parent = the domestic filing whose OWN jurisdiction is the home state (not just any domestic)
        parents = [r for r in fam_recs
                   if not self._biz_is_foreign(r)
                   and (r.get('FilingJurisdictionPostalAbbreviation') or '').upper() == home]
        parent = parents[0] if parents else None

        off_files = collections.defaultdict(set)
        for r in fam_recs:
            st = (r.get('FilingJurisdictionPostalAbbreviation') or '').upper()
            for p in (r.get('Principals') or []):
                nm = (p.get('PrincipalName') or '').strip()
                if nm:
                    off_files[nm.upper()].add(st)
        corr = sorted({k.title() for k, sts in off_files.items() if len(sts) > 1})

        lines = [f'ENTITY FAMILY "{name}" (home {home}) — {len(branch_states)} branch registrations']
        if parent:
            fd = ((parent.get('FilingDate') or {}).get('Date') or '')[:10]
            lines.append(f'  RECOMMEND (home entity): {parent.get("EntityName")} — {home} '
                         f'#{parent.get("FileNumber")} ({parent.get("FilingStatus")}'
                         f'{", " + fd if fd else ""})')
        else:
            lines.append(f'  Home/domestic parent is in {home} (not captured — infer the {home} filing)')
        lines.append(f'  Branches -> {home} ({len(branch_states)}): {", ".join(branch_states)}')
        if hq:
            lines.append(f'  Operating HQ (from branches): {hq.most_common(1)[0][0]}')
        if corr:
            lines.append(f'  Officers on >1 filing (identity lock): {", ".join(corr[:8])}')
        return "\n".join(lines)


def _json_decode(response: str | None):
    """Mirror PHP json_decode($response, true): returns parsed value or None."""
    if not response:
        return None
    try:
        return json.loads(response)
    except (ValueError, TypeError):
        return None
