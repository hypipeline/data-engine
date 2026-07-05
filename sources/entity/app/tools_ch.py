"""
Entity Lookup v3b (Python) — Companies House tool cluster.

Faithful like-for-like port of the Companies House methods in php/tools.php
(class LookupTools, methods ~571-1041):

    searchCompaniesHouse                -> search_companies_house
    lookupCompaniesHouseByNumber        -> lookup_companies_house_by_number
    companiesHouseCorporateAppointments -> companies_house_corporate_appointments
    companiesHouseBrandSearch           -> companies_house_brand_search
    companiesHouseGetOfficers           -> companies_house_get_officers
    companiesHouseGetCompany            -> companies_house_get_company
    companiesHouseOwnershipChain        -> companies_house_ownership_chain

    (private helpers)
    chApiGet          -> _ch_api_get
    looksLikeCompany  -> _looks_like_company

Companies House public web pages are fetched via the shared self.http_get() cascade
against find-and-update.company-information.service.gov.uk (HTML scraping). The
official REST API (api.company-information.service.gov.uk) is hit directly by
_ch_api_get, using HTTP Basic auth with the api_key as the username and an empty
password (exactly like the PHP CURLOPT_USERPWD "{apiKey}:" + CURLAUTH_BASIC).

This class is combined with ToolBase (toolbase.py) via multiple inheritance, so it
calls self.config, self._progress, self.html_to_text, self.http_get on the composed
instance. Return shapes match the PHP exactly (formatted strings or arrays -> dict/list).
"""
from __future__ import annotations

import html as _htmllib
import json
import re
from urllib.parse import quote, urlencode

import requests


class CompaniesHouseMixin:

    # ── internal log helper (PHP appends to private array $log) ──────────────
    def _ch_log(self, tool: str, input, output) -> None:
        entry = {'tool': tool, 'input': input, 'output': output}
        try:
            self.log.append(entry)
        except AttributeError:
            self.log = [entry]

    # ── Companies House ──────────────────────────────────────────────────────
    def search_companies_house(self, query: str) -> str:
        url = ("https://find-and-update.company-information.service.gov.uk/search?q="
               + quote(query, safe=''))
        html = self.http_get(url)
        if not html:
            return "Error: Could not fetch Companies House."

        results = []
        for match in re.finditer(
                r'<li class="type-company"[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>([^<]*)</a>',
                html, re.I | re.S):
            href = match.group(1)
            name = _htmllib.unescape(match.group(2)).strip()
            # Skip template placeholders
            if '{{' in name or len(name) < 2:
                continue
            if not href.startswith('/company/'):
                continue
            full_url = f"https://find-and-update.company-information.service.gov.uk{href}"
            results.append(f"{name} | {full_url}")
            if len(results) >= 10:
                break

        result = "\n".join(results) if results else "No Companies House results found."
        self._ch_log('search_companies_house', query, result)
        return result

    def lookup_companies_house_by_number(self, company_number: str):
        """Returns dict with name, status, etc. or None if not found."""
        url = ("https://find-and-update.company-information.service.gov.uk/company/"
               + quote(company_number, safe=''))
        html = self.http_get(url)
        if not html:
            return None

        # Extract company name from the page title or heading
        name = None
        m = re.search(r'<h1[^>]*class="[^"]*heading-xlarge[^"]*"[^>]*>\s*([^<]+)', html, re.I)
        if m:
            name = _htmllib.unescape(m.group(1)).strip()
        else:
            m = re.search(r'<title>([^<]+)</title>', html, re.I)
            if m:
                title = _htmllib.unescape(m.group(1)).strip()
                # Title format is usually "COMPANY NAME - Find and update company information"
                name = re.sub(r'\s*[-–].*$', '', title)
        if not name:
            return None

        # Extract company status
        status = None
        m = re.search(r'Company status\s*</dt>\s*<dd[^>]*>\s*([^<]+)', html, re.I)
        if m:
            status = _htmllib.unescape(m.group(1)).strip()

        self._ch_log('lookup_companies_house_by_number', company_number,
                     f"Name: {name}, Status: {status}")

        return {
            'company_name': name,
            'company_status': status,
            'company_number': company_number,
            'url': url,
        }

    def companies_house_corporate_appointments(self, entity_name: str) -> list:
        """
        Find companies where a UK entity serves as a corporate officer (director).
        Returns list of dicts with company_name, company_number, role, appointed,
        status ('active'|'resigned'), company_status.
        """
        # Step 1: Search officers index for the corporate entity
        search_url = ("https://find-and-update.company-information.service.gov.uk/search/officers?q="
                      + quote(entity_name, safe=''))
        html = self.http_get(search_url)
        if not html:
            self._ch_log('ch_corporate_appointments', entity_name,
                         'Error: could not fetch officers search')
            return []

        # Parse results — look for exact name match (case-insensitive)
        officer_id = None
        norm_target = entity_name.strip().upper()

        # Each result: <li> with <a href="/officers/{id}/appointments">{name}</a>
        for m in re.finditer(r'<a[^>]*href="/officers/([^"/]+)/appointments"[^>]*>\s*([^<]+)',
                             html, re.I):
            candidate_name = _htmllib.unescape(m.group(2)).strip().upper()
            if candidate_name == norm_target:
                officer_id = m.group(1)
                break

        if not officer_id:
            self._ch_log('ch_corporate_appointments', entity_name,
                         'No corporate officer match found')
            return []

        # Step 2: Fetch appointments page
        appointments_url = (f"https://find-and-update.company-information.service.gov.uk/"
                            f"officers/{officer_id}/appointments")
        html = self.http_get(appointments_url)
        if not html:
            self._ch_log('ch_corporate_appointments', entity_name,
                         'Error: could not fetch appointments page')
            return []

        # Parse appointments — split HTML by company links, each section is an appointment
        appointments = []
        sections = re.split(r'(?=<a[^>]*href="/company/\d+")', html)
        for section in sections:
            company_name = None
            company_number = None
            cm = re.search(r'<a[^>]*href="/company/(\d+)"[^>]*>\s*([^<]+)', section, re.I)
            if cm:
                company_number = cm.group(1)
                company_name = _htmllib.unescape(cm.group(2)).strip()
                # Strip company number in parentheses, e.g. "GLOBAL HOLDCO LIMITED (14194682)"
                company_name = re.sub(r'\s*\(\d+\)\s*$', '', company_name)
            if not company_name or not company_number or len(company_name) < 2:
                continue

            # Role — from <dd> after "Role" <dt>
            role = 'Director'
            rm = re.search(r'appointment-type-value\d*"[^>]*>\s*([^<]+)', section, re.I)
            if rm:
                role = rm.group(1).strip()

            # Appointment status — "Resigned" or "Active" in status-tag
            appointment_status = 'active'
            sm = re.search(r'class="status-tag[^"]*"[^>]*>\s*(Resigned|Active)\s*<', section, re.I)
            if sm:
                appointment_status = sm.group(1).strip().lower()

            # Company status — Active/Dissolved
            company_status = None
            csm = re.search(r'company-status-value[^>]*>\s*([^<]+)', section, re.I)
            if csm:
                company_status = csm.group(1).strip()

            # Appointed date
            appointed = None
            am = re.search(r'appointed-value\d*"[^>]*>\s*([^<]+)', section, re.I)
            if am:
                appointed = am.group(1).strip()

            appointments.append({
                'company_name': company_name,
                'company_number': company_number,
                'role': role,
                'appointed': appointed,
                'status': appointment_status,
                'company_status': company_status,
            })

        active_count = sum(1 for a in appointments if a['status'] == 'active')
        self._ch_log('ch_corporate_appointments', entity_name,
                     f"{len(appointments)} appointments found ({active_count} active) "
                     f"via officer ID {officer_id}")

        return appointments

    # ── Companies House Brand Search (API) ─────────────────────────────────
    def companies_house_brand_search(self, brand_name: str, known_postcodes: list,
                                     known_officers: list, known_company_numbers: list = None) -> list:
        """
        Search CH API for a brand/short name, then filter results to only those
        sharing a postcode or director with the known entities.
        """
        if known_company_numbers is None:
            known_company_numbers = []

        api_key = self.config.get('companies_house_api_key') or ''
        if not api_key:
            return []

        # Normalise known data for comparison
        known_postcodes_norm = [re.sub(r'\s+', '', p).upper() for p in known_postcodes]
        known_officers_norm = [o.upper() for o in known_officers]
        known_numbers_set = set(known_company_numbers)

        # Search CH API — fetch up to 40 results (2 pages)
        candidates = []
        for page in range(2):
            start_index = page * 20
            url = ("https://api.company-information.service.gov.uk/search/companies?"
                   + urlencode({'q': brand_name, 'items_per_page': 20, 'start_index': start_index}))
            js = self._ch_api_get(url, api_key)
            if not js:
                break
            data = json.loads(js)
            items = data.get('items') or []
            if not items:
                break
            for item in items:
                number = item.get('company_number') or ''
                status = item.get('company_status') or 'unknown'
                if number in known_numbers_set:
                    continue
                if status == 'dissolved':
                    continue
                postal_code = re.sub(r'\s+', '', (item.get('address') or {}).get('postal_code') or '').upper()
                candidates.append({
                    'company_name': item.get('title') or '',
                    'company_number': number,
                    'company_status': status,
                    'address': item.get('address_snippet') or '',
                    'postal_code': postal_code,
                })

        # Phase 1: Filter by postcode match
        matched = []
        need_officer_check = []
        for c in candidates:
            if c['postal_code'] and c['postal_code'] in known_postcodes_norm:
                c['match_reason'] = 'address (shared postcode)'
                matched.append(c)
            else:
                need_officer_check.append(c)

        # Phase 2: For non-postcode matches, check officers (limit API calls)
        officer_checks = 0
        max_officer_checks = 10
        for c in need_officer_check:
            if officer_checks >= max_officer_checks:
                break
            officer_checks += 1
            officers_url = (f"https://api.company-information.service.gov.uk/company/"
                            f"{c['company_number']}/officers?items_per_page=50")
            officers_json = self._ch_api_get(officers_url, api_key)
            if not officers_json:
                continue
            officers_data = json.loads(officers_json)
            officers = officers_data.get('items') or []
            officer_names = []
            for o in officers:
                if o.get('resigned_on'):
                    continue  # skip resigned
                name = (o.get('name') or '').upper()
                officer_names.append(name)
                # CH format is "SURNAME, Forename" — extract surname
                surname = name.split(',')[0].strip()
                role = o.get('officer_role') or 'officer'
                if surname and surname in known_officers_norm:
                    c['match_reason'] = f"shared {role} ({o.get('name')})"
                    c['officers'] = [o2.get('name') or '' for o2 in officers]
                    matched.append(c)
                    break

        self._ch_log('ch_brand_search', brand_name,
                     f"{len(candidates)} candidates, {len(matched)} matched "
                     f"(postcodes: {', '.join(known_postcodes)}, "
                     f"officers: {', '.join(known_officers)})")

        return matched

    def companies_house_get_officers(self, company_number: str) -> list:
        """Fetch company officers from CH API. Returns list of officer name strings."""
        api_key = self.config.get('companies_house_api_key') or ''
        if not api_key:
            return []
        url = (f"https://api.company-information.service.gov.uk/company/"
               f"{company_number}/officers?items_per_page=50")
        js = self._ch_api_get(url, api_key)
        if not js:
            return []
        data = json.loads(js)
        names = []
        for o in (data.get('items') or []):
            if o.get('resigned_on'):
                continue  # skip resigned
            names.append(o.get('name') or '')
        return names

    def companies_house_get_company(self, company_number: str):
        """Fetch company details from CH API. Returns dict or None."""
        api_key = self.config.get('companies_house_api_key') or ''
        if not api_key:
            return None
        url = f"https://api.company-information.service.gov.uk/company/{company_number}"
        js = self._ch_api_get(url, api_key)
        if not js:
            return None
        data = json.loads(js)
        roa = data.get('registered_office_address') or {}
        return {
            'company_name': data.get('company_name') or '',
            'company_number': data.get('company_number') or company_number,
            'company_status': data.get('company_status') or 'unknown',
            'postal_code': roa.get('postal_code') or '',
            'address': ', '.join(x for x in [
                roa.get('address_line_1') or '',
                roa.get('locality') or '',
                roa.get('postal_code') or '',
            ] if x),
        }

    def _ch_api_get(self, url: str, api_key: str):
        try:
            r = requests.get(url, auth=(api_key, ''), timeout=10)
        except requests.RequestException:
            return None
        return r.text if (r.status_code == 200 and r.text is not None) else None

    # ── Companies House Ownership Chain ──────────────────────────────────────
    def companies_house_ownership_chain(self, company_number: str) -> str:
        chain = []
        visited = []
        current = company_number

        for level in range(self.config['max_ownership_levels']):
            if current in visited:
                chain.append(f"  [circular reference to {current}]")
                break
            visited.append(current)

            # Fetch company overview for name
            overview_url = (f"https://find-and-update.company-information.service.gov.uk/"
                            f"company/{current}")
            html = self.http_get(overview_url)
            company_name = current
            if html:
                m = re.search(r'<h1[^>]*>([^<]+)</h1>', html, re.I)
                if m:
                    company_name = _htmllib.unescape(m.group(1)).strip()
            confirmed = ' (confirmed)' if (html and company_name != current) else ''
            chain.append(f"{company_name} (#{current}){confirmed} | {overview_url}")

            # Fetch PSC page
            psc_url = (f"https://find-and-update.company-information.service.gov.uk/"
                       f"company/{current}/persons-with-significant-control")
            psc_html = self.http_get(psc_url)
            if not psc_html:
                chain.append("  [TOP OF CHAIN]")
                break

            psc_text = self.html_to_text(psc_html)
            lines = psc_text.split("\n")

            # Parse the first Active corporate PSC entry
            in_active_psc = False
            psc_name = ''
            psc_reg_num = ''
            psc_ownership = ''
            psc_incorporated_in = ''

            for i in range(len(lines)):
                line = lines[i].strip()

                if not in_active_psc and line == 'Active' and i > 0:
                    prev = lines[i - 1].strip()
                    if self._looks_like_company(prev):
                        in_active_psc = True
                        psc_name = prev
                        continue

                if not in_active_psc:
                    continue

                # Collect fields
                if line == 'Registration number' and i + 1 < len(lines):
                    psc_reg_num = lines[i + 1].strip()
                if line.startswith('Incorporated in') and i + 1 < len(lines):
                    psc_incorporated_in = lines[i + 1].strip().lower()
                elif line.lower().startswith('incorporated in'):
                    parts = line.split('in', 1)
                    psc_incorporated_in = (parts[1] if len(parts) > 1 else '').strip().lower()
                if 'ownership of shares' in line.lower():
                    psc_ownership = line.strip()
                # End of PSC entry
                if line in ('Ceased', 'Ceased on') and i > 0:
                    break
                if line == 'Active' and psc_reg_num:
                    break

            corporate_owner = None

            if in_active_psc and psc_reg_num:
                # Check ownership > 50%
                ownership_lower = psc_ownership.lower()
                owns_majority = ('75%' in ownership_lower or
                                 ('more than 50%' in ownership_lower and
                                  'not more than 50%' not in ownership_lower))
                if not psc_ownership:
                    owns_majority = True  # No info = assume majority

                # Check UK-registered
                prefix = psc_reg_num[:2].upper()
                is_uk = self._ctype_digit(psc_reg_num) or prefix in ('OC', 'SC', 'NI', 'SO', 'NC')
                if psc_incorporated_in and psc_incorporated_in not in (
                        'uk', 'united kingdom', 'england', 'wales',
                        'scotland', 'northern ireland', 'england and wales'):
                    is_uk = False

                if owns_majority and is_uk:
                    if self._ctype_digit(psc_reg_num):
                        psc_reg_num = psc_reg_num.rjust(8, '0')
                    else:
                        psc_reg_num = psc_reg_num.upper()
                    corporate_owner = psc_reg_num
                    chain.append(f"  ↑ owned ({psc_ownership}) by: {psc_name} (#{psc_reg_num})")
                elif not owns_majority:
                    chain.append(f"  [STOP: {psc_name} owns ≤50%: {psc_ownership}]")
                elif owns_majority and not is_uk:
                    # Non-UK parent — record it as unconfirmed, stop here
                    country = psc_incorporated_in or 'unknown'
                    chain.append(f"  ↑ owned ({psc_ownership}) by: {psc_name} "
                                 f"(#{psc_reg_num}, incorporated in: {country}) (unconfirmed)")
                    chain.append("  [TOP OF CHAIN — non-UK entity, cannot trace further via Companies House]")
                    break

            if corporate_owner:
                current = corporate_owner
            else:
                # No corporate owner — list individuals
                individuals = []
                for i in range(len(lines)):
                    if lines[i].strip() == 'Active' and i > 0:
                        prev = lines[i - 1].strip()
                        if re.match(r'^(Mr|Ms|Mrs|Dr)\s', prev):
                            individuals.append(prev)
                if individuals:
                    chain.append("  ↑ owned by individuals: " + ', '.join(individuals))
                chain.append("  [TOP OF CHAIN]")
                break

        result = "\n".join(chain)
        self._ch_log('companies_house_ownership_chain', company_number, result)
        return result

    def _looks_like_company(self, name: str) -> bool:
        lower = name.lower()
        for suffix in ('ltd', 'limited', 'llp', 'plc', 'inc', 'ag', 'gmbh', 'sa', 'sarl',
                       'bv', 'nv', 'se', 'srl', 'spa', 'as', 'aps', 'ab', 'oy', 'corp',
                       'llc', 'lp'):
            if re.search(r'\b' + re.escape(suffix) + r'\b', lower):
                return True
        return False

    @staticmethod
    def _ctype_digit(s: str) -> bool:
        # PHP ctype_digit: True only for a non-empty string of ASCII digits.
        return bool(re.fullmatch(r'\d+', s))
