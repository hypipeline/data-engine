"""
Entity Lookup v3b (Python) — Phase 4 registry-search dispatch.

Faithful port of the REGISTRY-SEARCH group of php/lookup.php:
    searchRegistries, discoverAndSearchNewEntities, normaliseEntityName,
    matchEntityName, searchRegistriesForName, registryKeyToSource.

Provided as a mixin (RegistrySearchMixin) combined into EntityLookup via multiple
inheritance. Like-for-like: registry routing, per-name loops, max_entity_names /
max_ciks caps, dedup, ordering and every self.log(...) message are preserved.

On `self` these are CALLED (defined in agent.py / sibling mixins, not here):
    self.config, self.log, self.deduplicate_names, self.extract_candidate_names,
    self.log_registry_result   (PHP logRegistryResult)
and the registry tools on self.tools (snake_case of the PHP tool names), incl.
    self.tools.sort_bizapedia_results  (PHP LookupTools::sortBizapediaResults, in-place)
    self.tools.deduplicate_bizapedia_results  (PHP LookupTools::deduplicateBizapediaResults)
    self.tools.fetch_sec  (PHP fetchSec8K — 8-K cover-page fetch: fetch_sec(cik, submissions))

stdlib only (json, re, urllib.parse).
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote_plus


# ── Entity-name abbreviation table (PHP static $abbreviations) ───────────────
_ABBREVIATIONS = {
    # Legal structure — English
    'inc': 'incorporated', 'corp': 'corporation', 'ltd': 'limited',
    'co': 'company', 'plc': 'public limited company',
    'llc': 'limited liability company', 'l.l.c.': 'limited liability company',
    'llp': 'limited liability partnership', 'l.l.p.': 'limited liability partnership',
    'lp': 'limited partnership', 'l.p.': 'limited partnership',
    'pty': 'proprietary',
    # Legal structure — German
    'ag': 'aktiengesellschaft',
    'gmbh': 'gesellschaft mit beschränkter haftung',
    'kg': 'kommanditgesellschaft',
    'ohg': 'offene handelsgesellschaft',
    'eg': 'eingetragene genossenschaft',
    'se': 'societas europaea',
    # Legal structure — French
    'sa': 'société anonyme',
    'sas': 'société par actions simplifiée',
    'sarl': 'société à responsabilité limitée',
    # Legal structure — Dutch
    'bv': 'besloten vennootschap',
    'nv': 'naamloze vennootschap',
    # Legal structure — Nordic
    'ab': 'aktiebolag',
    'as': 'aksjeselskap',
    'aps': 'anpartsselskab',
    'oy': 'osakeyhtiö',
    # Legal structure — Italian/Spanish
    'srl': 'società a responsabilità limitata',
    'spa': 'società per azioni',
    'sl': 'sociedad limitada',
    # Business terms
    'assoc': 'association', 'assn': 'association',
    'bros': 'brothers',
    'intl': 'international', "int'l": 'international',
    'natl': 'national', "nat'l": 'national',
    'mgmt': 'management', 'mgt': 'management',
    'svcs': 'services', 'svc': 'service',
    'grp': 'group',
    'hldgs': 'holdings', 'hldg': 'holding',
    'mfg': 'manufacturing',
    'dept': 'department',
    'dist': 'distribution',
    'tech': 'technology',
    'fin': 'financial',
    'dev': 'development',
    'invt': 'investment', 'inv': 'investment',
    'props': 'properties', 'prop': 'property',
    'sys': 'systems',
    'indus': 'industries', 'ind': 'industries',
    'engr': 'engineering', 'eng': 'engineering',
    'pharm': 'pharmaceutical', 'pharma': 'pharmaceutical',
    'chem': 'chemical',
    'elec': 'electric', 'electr': 'electronic',
    'telecom': 'telecommunications',
    'transp': 'transportation',
    'ins': 'insurance',
    'bancorp': 'banking corporation',
    'mtg': 'mortgage',
    'realty': 'realty', 'rlty': 'realty',
}

# reverse map (long → short), built once (PHP static $reverse in normaliseEntityName)
_ABBREV_REVERSE = {}
for _short, _long in _ABBREVIATIONS.items():
    _ABBREV_REVERSE[_long] = _short


# Jurisdiction routing sets (PHP array_intersect targets)
_US_JUR = {'us', 'united states', 'delaware', 'new york', 'california'}
_UK_JUR = {'uk', 'england', 'scotland', 'wales', 'united kingdom'}
_EU_JUR = {'germany', 'france', 'netherlands', 'austria', 'switzerland', 'europe',
           'finland', 'denmark', 'sweden', 'norway', 'poland', 'czech republic',
           'belgium', 'luxembourg', 'italy', 'spain', 'ireland'}


def _co(value, default):
    """PHP `?? default` semantics: replace only null/unset (None), not '' or 0."""
    return default if value is None else value


def _json_loads(text):
    """PHP json_decode($x, true): return parsed value or None on failure."""
    try:
        return json.loads(text)
    except Exception:
        return None


class RegistrySearchMixin:
    # ── Phase 4: searchRegistries ───────────────────────────────────────────
    def search_registries(self, entity_info: dict, domain: str) -> dict:
        unique_names = entity_info.get('entity_names') or []
        unique_names = unique_names[:self.config['max_entity_names']]
        short_names = entity_info.get('short_names') or []
        jurisdiction = (entity_info.get('jurisdiction') or 'unknown').lower()
        registries: dict = {}

        # Build list of jurisdictions to search
        jurisdictions = [jurisdiction]
        if entity_info.get('known_jurisdiction'):
            known_jur = entity_info['known_jurisdiction'].lower()
            if known_jur != jurisdiction:
                jurisdictions.append(known_jur)

        jset = set(jurisdictions)
        is_us = jset & _US_JUR
        is_uk = jset & _UK_JUR
        is_eu = jset & _EU_JUR
        is_unknown = 'unknown' in jurisdictions

        # ── Declare search plan ──
        registry_sources = []
        if is_uk or is_unknown:
            registry_sources.append('Companies House')
        if is_us or is_unknown:
            registry_sources.append('SEC EDGAR')
        if is_us or is_unknown:
            registry_sources.append('SEC IAPD')
        if is_us or is_unknown:
            registry_sources.append('Bizapedia')
        if is_us or is_unknown:
            registry_sources.append('Delaware Div. of Corps.')
        if is_eu or is_uk or is_unknown:
            registry_sources.append('North Data')
        if is_us or is_unknown:
            registry_sources.append('EDGAR Exhibit 21')

        self.log('registry', "Search plan: " + str(len(unique_names)) + " entity name(s) + "
                 + str(len(short_names)) + " short name(s) × " + str(len(registry_sources)) + " registries", {
                     'names': unique_names,
                 })
        self.log('registry', "  Jurisdictions: " + ', '.join(jurisdictions))
        self.log('registry', "  Entity names: " + json.dumps(unique_names))
        if short_names:
            self.log('registry', "  Short names: " + json.dumps(short_names))
        self.log('registry', "  Registries: " + ', '.join(registry_sources))

        # ── Build combined search list: entity names + short names (deduplicated) ──
        all_search_names = list(unique_names)
        short_name_set = {}  # track which are short names (for trademark search)
        for sn in short_names:
            sn_norm = sn.strip().lower()
            is_duplicate = False
            for existing in all_search_names:
                if existing.strip().lower() == sn_norm:
                    is_duplicate = True
                    break
            if not is_duplicate:
                all_search_names.append(sn)
            short_name_set[sn_norm] = True

        if len(all_search_names) > len(unique_names):
            self.log('registry', "  Combined search list (" + str(len(all_search_names))
                     + " names including short names): " + json.dumps(all_search_names))

        # ── Search entity by entity ──
        northdata_network_done = False

        for idx, name in enumerate(all_search_names):
            num = idx + 1
            total = len(all_search_names)
            is_short_name = name.strip().lower() in short_name_set
            label = f"{name} (short name)" if is_short_name else name
            self.log('entity_header', label, {'entity_num': num, 'entity_total': total})

            # Companies House (UK)
            if is_uk or is_unknown:
                registries[f"companies_house:{name}"] = self.tools.search_companies_house(name)
                self.log_registry_result('ch', 'Companies House', name, registries[f"companies_house:{name}"], name)

                # Trace ownership chain from first CH result with a company number
                if 'ownership_chain' not in registries and \
                        'find-and-update.company-information.service.gov.uk/company/' in registries[f"companies_house:{name}"]:
                    m = re.search(r'/company/(\w+)', registries[f"companies_house:{name}"])
                    if m:
                        self.log('ch', f"Tracing ownership chain from #{m.group(1)}...", {'entity_name': name})
                        registries['ownership_chain'] = self.tools.companies_house_ownership_chain(m.group(1))
                        chain_lines = registries['ownership_chain'].count('\n') + 1
                        self.log('ch', f"Ownership chain complete ({chain_lines} lines)", {
                            'entity_name': name,
                            'expandable': True,
                            'sections': [{'label': 'Full Chain', 'content': registries['ownership_chain']}],
                        })

                # Corporate appointments — find companies where this entity is a corporate director
                if 'ch_corporate_appointments' not in registries:
                    self.log('ch', f"Looking up corporate appointments for \"{name}\"...", {'entity_name': name})
                    appointments = self.tools.companies_house_corporate_appointments(name)
                    if appointments:
                        active = [a for a in appointments
                                  if a['status'] == 'active' and (a.get('company_status') or '').lower() != 'dissolved']
                        resigned = [a for a in appointments
                                    if a['status'] != 'active' or (a.get('company_status') or '').lower() == 'dissolved']
                        lines = [f"Companies where \"{name}\" serves as a corporate officer (i.e. the company itself is appointed as a director/secretary of other companies):"]
                        lines.append("")
                        for appt in appointments:
                            status = _co(appt.get('status'), 'unknown')
                            company_status = _co(appt.get('company_status'), 'unknown')
                            lines.append(f"- {appt['company_name']} (#{appt['company_number']}) — role: {appt['role']}, appointment: {status}, company status: {company_status}")
                        registries['ch_corporate_appointments'] = "\n".join(lines)
                        # Build a chatty summary for the log
                        active_names = [a['company_name'] for a in active]
                        summary = f"\"{name}\" is a corporate officer of " + str(len(appointments)) + " companies"
                        if len(active) > 0:
                            summary += " (" + str(len(active)) + " active"
                            if len(resigned) > 0:
                                summary += ", " + str(len(resigned)) + " resigned/dissolved"
                            summary += ")"
                        if len(active_names) <= 5:
                            summary += ": " + ', '.join(active_names)
                        else:
                            summary += ": " + ', '.join(active_names[:4]) + " + " + str(len(active_names) - 4) + " more"
                        self.log('ch', summary, {'entity_name': name, 'expandable': True,
                                                 'sections': [{'label': 'All Appointments', 'content': registries['ch_corporate_appointments']}]})
                    else:
                        self.log('ch', f"No corporate appointments found — \"{name}\" is not a director of other companies", {'entity_name': name})

            # SEC EDGAR company search (US)
            if is_us or is_unknown:
                registries[f"sec_company:{name}"] = self.tools.search_sec_company(name)
                self.log_registry_result('sec', 'SEC Company', name, registries[f"sec_company:{name}"], name)

            # SEC IAPD (US)
            if is_us or is_unknown:
                iapd_result = self.tools.search_sec_iapd(name)
                registries[f"sec_iapd:{name}"] = iapd_result
                self.log_registry_result('sec_iapd', 'SEC IAPD', name, iapd_result, name)

            # Bizapedia (US)
            if is_us or is_unknown:
                biz_results = self.tools.search_bizapedia(name)
                if biz_results:
                    self.tools.sort_bizapedia_results(biz_results)
                    biz_json = json.dumps(biz_results, indent=4, ensure_ascii=False)
                    registries[f"bizapedia:{name}"] = biz_json[:5000]
                self.log_registry_result('bizapedia', 'Bizapedia', name, str(len(biz_results)) + ' results', name)

                # Delaware Division of Corporations
                delaware_result = self.tools.search_delaware(name)
                registries[f"delaware:{name}"] = delaware_result
                self.log_registry_result('delaware', 'Delaware', name, delaware_result, name)

            # North Data (EU + UK — provides financial data for UK companies)
            if is_eu or is_uk or is_unknown:
                registries[f"northdata:{name}"] = self.tools.search_northdata(name)
                self.log_registry_result('northdata', 'North Data', name, registries[f"northdata:{name}"], name)

                # Follow up on first NorthData result to get ownership graph
                if not northdata_network_done and 'No North Data results' not in registries[f"northdata:{name}"] \
                        and not registries[f"northdata:{name}"].startswith('Error:'):
                    # Extract URL — from search results (→ url) or reconstruct from direct page
                    nd_url = None
                    url_match = re.search(r'→ (https://www\.northdata\.com/[^\s]+)', registries[f"northdata:{name}"])
                    if url_match:
                        nd_url = url_match.group(1)
                    elif '=== NorthData Company Page:' in registries[f"northdata:{name}"]:
                        nd_url = "https://www.northdata.com/" + quote_plus(name)
                    if nd_url:
                        self.log('northdata', "Loading ownership graph via Browserbase...", {'entity_name': name})
                        network_result = self.tools.northdata_network(nd_url)
                        registries['northdata_network'] = network_result
                        northdata_network_done = True

                        nd_summary = "Network graph loaded"
                        if 'OWNED BY' in network_result:
                            nd_summary = "Ownership structure found — parent entities identified"
                        elif 'ultimate parent/TopCo' in network_result:
                            nd_summary = "Entity appears to be TopCo — no parent above it"
                        self.log('northdata', f"Network: {nd_summary}", {
                            'entity_name': name,
                            'expandable': True,
                            'sections': [{'label': 'Full Network', 'content': network_result}],
                        })

            # EDGAR Exhibit 21 parent search (US)
            if is_us or is_unknown:
                edgar_result = self.tools.edgar_parent_search(name)
                registries[f"edgar_parent:{name}"] = edgar_result

                edgar_summary = "No parent found"
                if 'EDGAR PARENT FOUND' in edgar_result:
                    parent = ''
                    pm = re.search(r'Parent:\s*(.+)', edgar_result)
                    if pm:
                        parent = pm.group(1).strip()
                    confirmed = 'STRONG evidence' in edgar_result
                    edgar_summary = f"subsidiary of {parent}" + (" (confirmed)" if confirmed else "")
                elif 'EDGAR MENTION ONLY' in edgar_result:
                    filer = ''
                    pm = re.search(r'filings of:\s*(.+)', edgar_result)
                    if pm:
                        filer = pm.group(1).strip()
                    edgar_summary = f"mentioned in filings by {filer} (weak)"
                self.log('edgar', f"Exhibit 21: \"{name}\" → {edgar_summary}", {
                    'entity_name': name,
                    'expandable': True,
                    'sections': [{'label': 'Full Result', 'content': edgar_result}],
                })

        # ── Trademark search for short_names (US only, additional to registry searches) ──
        if (is_us or is_unknown) and short_names:
            self.log('entity_header', 'Trademark Searches', {'entity_num': 'TM', 'entity_total': str(len(short_names)) + ' names'})
            for sn in short_names:
                tm_result = self.tools.search_bizapedia_trademark(sn)
                registries[f"trademark:{sn}"] = tm_result
                self.log_registry_result('bizapedia', 'Bizapedia TM', sn, tm_result, sn)

        # ── Brand name search on Companies House (UK only) ──
        # Search for short/brand names, filter by shared postcode or director with known entities
        if (is_uk or is_unknown) and short_names:
            # Collect known company numbers from CH results
            known_company_numbers = []
            for key, val in registries.items():
                found = re.findall(r'/company/([A-Z0-9]+)', val)
                if found:
                    known_company_numbers.extend(found)
            known_company_numbers = _unique(known_company_numbers)

            # Fetch address + officers for each known company via CH API
            known_postcodes = []
            known_officers = []
            self.log('ch', "Collecting addresses and officers from " + str(len(known_company_numbers)) + " known CH companies for brand search cross-reference...")
            for num in known_company_numbers[:5]:
                co = self.tools.companies_house_get_company(num)
                if co and co['postal_code']:
                    known_postcodes.append(co['postal_code'])
                    self.log('ch', f"  #{num} ({co['company_name']}): {co['address']}")
                officers = self.tools.companies_house_get_officers(num)
                for o_name in officers:
                    surname = o_name.split(',')[0].strip()
                    if surname:
                        known_officers.append(surname)
                if officers:
                    self.log('ch', f"  #{num} officers: " + ', '.join(officers[:5]))
            known_postcodes = _unique(known_postcodes)
            known_officers = _unique(known_officers)

            if known_postcodes or known_officers:
                for sn in short_names:
                    if f"ch_brand_search:{sn}" in registries:
                        continue
                    self.log('ch', f"Brand search: \"{sn}\" — matching against " + str(len(known_postcodes)) + " postcodes, " + str(len(known_officers)) + " officer surnames")
                    matches = self.tools.companies_house_brand_search(sn, known_postcodes, known_officers, known_company_numbers)
                    if matches:
                        lines = [f"Companies House companies matching brand name \"{sn}\" that share an address or director with known entities:"]
                        lines.append("")
                        for match in matches:
                            lines.append(f"- {match['company_name']} (#{match['company_number']}) — status: {match['company_status']}, address: {match['address']}, matched by: {match['match_reason']}")
                        registries[f"ch_brand_search:{sn}"] = "\n".join(lines)
                        match_names = [m['company_name'] for m in matches]
                        summary = str(len(matches)) + f" related \"{sn}\" companies found: " + (
                            ', '.join(match_names) if len(match_names) <= 5
                            else ', '.join(match_names[:4]) + " + " + str(len(match_names) - 4) + " more")
                        self.log('ch', summary, {'expandable': True,
                                                 'sections': [{'label': 'All Matches', 'content': registries[f"ch_brand_search:{sn}"]}]})
                    else:
                        self.log('ch', f"Brand search: no related companies found for \"{sn}\"")
            else:
                self.log('ch', "Brand search skipped — no known postcodes or officers to cross-reference")

        # ── Domain-level SEC searches (US only) ──
        if is_us or is_unknown:
            self.log('entity_header', "SEC Domain & Filing Searches", {'entity_num': 'SEC', 'entity_total': domain})

            # SEC fulltext search by domain
            registries[f"sec_fulltext:{domain}"] = self.tools.search_sec_fulltext(domain)
            self.log_registry_result('sec', 'SEC Fulltext', domain, registries[f"sec_fulltext:{domain}"])

            # Fetch SEC submissions for any CIKs found across all results
            ciks = []
            for key, val in registries.items():
                m = re.findall(r'CIK:\s*(\d+)', val)
                if m:
                    ciks.extend(m)
                if 'sec_fulltext' in key:
                    m2 = re.findall(r'CIK\s*(\d+)', val)
                    if m2:
                        ciks.extend(m2)
            ciks = _unique(ciks)
            ciks = ciks[:self.config['max_ciks']]

            if ciks:
                self.log('sec', "Fetching submissions for " + str(len(ciks)) + " CIK(s): " + ', '.join(ciks))

            for cik in ciks:
                submissions = self.tools.fetch_sec_submissions(cik)
                registries[f"sec_submissions:{cik}"] = submissions
                sub_data = _json_loads(submissions)
                _name = (sub_data or {}).get('name')
                entity_name = _name if _name is not None else 'unknown'
                total_filings = _co((sub_data or {}).get('total_filings'), '')
                self.log('sec', f"CIK {cik} → {entity_name} ({total_filings} filings)", {
                    'expandable': True,
                    'sections': [{'label': 'Full Submissions', 'content': submissions}],
                })

                if sub_data and 'latest_filings' in sub_data:
                    # Fetch 8-K cover page for structured entity data
                    eight_k = self.tools.fetch_sec(cik, sub_data)
                    if eight_k:
                        registries[f"sec_8k:{cik}"] = json.dumps(eight_k, indent=4)
                        reg_name = _co(eight_k.get('registered_name'), 'unknown')
                        state = _co(eight_k.get('state_of_incorporation'), '')
                        ein = _co(eight_k.get('irs_ein'), '')
                        self.log('sec', f"8-K: {reg_name} — incorporated in {state}, EIN {ein}", {
                            'expandable': True,
                            'sections': [{'label': '8-K Cover Page', 'content': registries[f"sec_8k:{cik}"]}],
                        })

                    # Look for Form D filings
                    for filing in sub_data['latest_filings']:
                        if _co(filing.get('form'), '') == 'D' and filing.get('primaryDocument'):
                            accession = filing['accession'].replace('-', '')
                            doc = filing['primaryDocument']
                            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"
                            registries[f"sec_filing:{cik}:FormD"] = self.tools.fetch_sec_filing(filing_url)
                            self.log('sec', f"Form D: CIK {cik}", {
                                'expandable': True,
                                'sections': [{'label': 'Filing Content', 'content': registries[f"sec_filing:{cik}:FormD"]}],
                            })
                            break

                # Fetch XBRL financial data (revenue, assets, etc.)
                financials = self.tools.sec_edgar_financials(cik)
                if financials:
                    registries[f"sec_financials:{cik}"] = financials
                    line_count = financials.count('\n')
                    self.log('sec', f"XBRL Financials: CIK {cik} ({line_count} lines)", {
                        'expandable': True,
                        'sections': [{'label': 'Financial Data', 'content': financials}],
                    })

        return registries

    # ── Phase 4 discovery: discoverAndSearchNewEntities ─────────────────────
    def discover_and_search_new_entities(self, registries: dict, entity_info: dict, domain: str) -> dict:
        # Collect all text from registry results
        registry_text = "\n".join(registries.values())

        # Run regex extraction on registry text (higher limit to catch all candidates)
        new_candidates = self.extract_candidate_names(registry_text, 20)

        # Build set of already-searched names (normalised for comparison)
        already_searched = (entity_info.get('entity_names') or []) + (entity_info.get('short_names') or [])
        already_norm = [n.strip(' .').lower() for n in already_searched]

        # Filter to genuinely new names
        new_names = []
        for candidate in new_candidates:
            norm = candidate.strip(' .').lower()
            # Skip if already searched or is a substring/superstring of an existing name
            dominated = False
            for existing in already_norm:
                if norm == existing or norm in existing or existing in norm:
                    dominated = True
                    break
            if not dominated and norm not in [n.strip(' .').lower() for n in new_names]:
                new_names.append(candidate)

        # Deduplicate the new names
        new_names = self.deduplicate_names(new_names)

        # Cap to avoid excessive searches
        new_names = new_names[:5]

        if not new_names:
            self.log('registry', "No new entity names discovered from registry data")
            return registries

        self.log('registry', "Discovered " + str(len(new_names)) + " new entity names from registry data: " + json.dumps(new_names))

        # Determine jurisdiction flags (same logic as searchRegistries)
        jurisdiction = (entity_info.get('jurisdiction') or 'unknown').lower()
        jurisdictions = [jurisdiction]
        if entity_info.get('known_jurisdiction'):
            known_jur = entity_info['known_jurisdiction'].lower()
            if known_jur != jurisdiction:
                jurisdictions.append(known_jur)
        jset = set(jurisdictions)
        is_us = jset & _US_JUR
        is_uk = jset & _UK_JUR
        is_eu = jset & _EU_JUR
        is_unknown = 'unknown' in jurisdictions

        bizapedia_all = []

        for name in new_names:
            self.log('registry', f"Searching new entity: \"{name}\"")

            if is_uk or is_unknown:
                registries[f"companies_house:{name}"] = self.tools.search_companies_house(name)
                self.log_registry_result('ch', 'Companies House', name, registries[f"companies_house:{name}"])
            if is_us or is_unknown:
                registries[f"sec_company:{name}"] = self.tools.search_sec_company(name)
                self.log_registry_result('sec', 'SEC Company', name, registries[f"sec_company:{name}"])

                iapd_result = self.tools.search_sec_iapd(name)
                registries[f"sec_iapd:{name}"] = iapd_result
                self.log_registry_result('sec_iapd', 'SEC IAPD', name, iapd_result)

                biz_results = self.tools.search_bizapedia(name)
                bizapedia_all = bizapedia_all + biz_results
                self.log_registry_result('bizapedia', 'Bizapedia', name, str(len(biz_results)) + ' results')

                if f"delaware:{name}" not in registries:
                    delaware_result = self.tools.search_delaware(name)
                    registries[f"delaware:{name}"] = delaware_result
                    self.log_registry_result('delaware', 'Delaware', name, delaware_result)
            if is_eu or is_unknown:
                registries[f"northdata:{name}"] = self.tools.search_northdata(name)
                self.log_registry_result('northdata', 'North Data', name, registries[f"northdata:{name}"])

        # Merge new Bizapedia results into the existing deduplicated set
        if bizapedia_all:
            existing_bizapedia = []
            if 'bizapedia' in registries:
                existing_bizapedia = _json_loads(registries['bizapedia']) or []
            # Re-deduplicate with both old and new raw results combined
            # We need the raw results for dedup, so convert existing compact records aren't raw —
            # just append new raw and re-dedup the whole thing
            all_raw = bizapedia_all
            # Add existing file numbers to seen set via the dedup function
            new_deduped = self.tools.deduplicate_bizapedia_results(all_raw)
            new_parsed = _json_loads(new_deduped) or []

            # Merge: existing + new (skip duplicates by file_number+jurisdiction_code)
            seen = {}
            for e in existing_bizapedia:
                key = _co(e.get('jurisdiction_code'), '') + ':' + _co(e.get('file_number'), '')
                seen[key] = True
            for n in new_parsed:
                key = _co(n.get('jurisdiction_code'), '') + ':' + _co(n.get('file_number'), '')
                if key not in seen:
                    existing_bizapedia.append(n)
                    seen[key] = True

            registries['bizapedia'] = json.dumps(existing_bizapedia, indent=4, ensure_ascii=False)
            self.log('registry', "Bizapedia updated: now " + str(len(existing_bizapedia)) + " unique entities (+" + str(len(new_parsed)) + " from discovery)", {
                'expandable': True,
                'sections': [{'label': 'Bizapedia Entities', 'content': registries['bizapedia']}],
            })

        return registries

    # ── Entity name normalisation ───────────────────────────────────────────
    def normalise_entity_name(self, name: str) -> list:
        """Generate all normalised variants of an entity name.
        Applies: punctuation removal, & ↔ and, abbreviation expansion/contraction."""
        base = name.strip().lower()
        # Remove periods and extra commas/spaces
        clean = base.replace('.', '')
        clean = re.sub(r'\s*,\s*', ' ', clean)
        clean = re.sub(r'\s+', ' ', clean.strip())

        variants = [base, clean]

        # & ↔ and
        if ' & ' in clean:
            variants.append(clean.replace(' & ', ' and '))
        elif ' and ' in clean:
            variants.append(clean.replace(' and ', ' & '))

        # For each variant so far, expand abbreviations and contract full words
        expanded = list(variants)
        for v in expanded:
            words = v.split(' ')

            # Try expanding (short → long)
            exp_words = list(words)
            changed = False
            for i, w in enumerate(exp_words):
                if w in _ABBREVIATIONS:
                    exp_words[i] = _ABBREVIATIONS[w]
                    changed = True
            if changed:
                variants.append(' '.join(exp_words))

            # Try contracting (long → short)
            con_words = list(words)
            changed = False
            for i, w in enumerate(con_words):
                if w in _ABBREV_REVERSE:
                    con_words[i] = _ABBREV_REVERSE[w]
                    changed = True
            if changed:
                variants.append(' '.join(con_words))

        return list(dict.fromkeys(variants))

    def match_entity_name(self, entity_name: str, website_text: str):
        """Check if any normalised variant of the entity name appears on the website.
        Returns the matched variant or None."""
        variants = self.normalise_entity_name(entity_name)
        for variant in variants:
            # PHP strlen() is a byte count; match that for the > 3 threshold
            if len(variant.encode('utf-8')) > 3 and variant in website_text:
                return variant
        return None

    # ── Re-analysis registry search: searchRegistriesForName ────────────────
    def search_registries_for_name(self, name: str, entity_info: dict, existing_registries: dict) -> dict:
        jurisdiction = (entity_info.get('jurisdiction') or 'unknown').lower()
        jurisdictions = [jurisdiction]
        if entity_info.get('known_jurisdiction'):
            known_jur = entity_info['known_jurisdiction'].lower()
            if known_jur != jurisdiction:
                jurisdictions.append(known_jur)
        jset = set(jurisdictions)
        is_us = jset & _US_JUR
        is_uk = jset & _UK_JUR
        is_eu = jset & _EU_JUR
        is_unknown = 'unknown' in jurisdictions

        results: dict = {}

        if is_uk or is_unknown:
            if f"companies_house:{name}" not in existing_registries:
                results[f"companies_house:{name}"] = self.tools.search_companies_house(name)
                self.log_registry_result('reanalysis', 'Companies House', name, results[f"companies_house:{name}"])

        if is_us or is_unknown:
            if f"sec_company:{name}" not in existing_registries:
                results[f"sec_company:{name}"] = self.tools.search_sec_company(name)
                self.log_registry_result('reanalysis', 'SEC Company', name, results[f"sec_company:{name}"])

            biz_results = self.tools.search_bizapedia(name)
            if biz_results:
                results[f"bizapedia:{name}"] = self.tools.deduplicate_bizapedia_results(biz_results)
                self.log_registry_result('reanalysis', 'Bizapedia', name, str(len(biz_results)) + ' results')

            if f"delaware:{name}" not in existing_registries:
                delaware_result = self.tools.search_delaware(name)
                results[f"delaware:{name}"] = delaware_result
                self.log_registry_result('reanalysis', 'Delaware', name, delaware_result)

        if is_eu or is_unknown:
            if f"northdata:{name}" not in existing_registries:
                results[f"northdata:{name}"] = self.tools.search_northdata(name)
                self.log_registry_result('reanalysis', 'North Data', name, results[f"northdata:{name}"])

        return results

    # ── Registry key → source-label mapping: registryKeyToSource ────────────
    def registry_key_to_source(self, key: str) -> str:
        if key.startswith('companies_house:'):
            return 'https://find-and-update.company-information.service.gov.uk/'
        if key.startswith('sec_company:'):
            return 'https://efts.sec.gov/LATEST/search-index?q=' + quote_plus(key[12:])
        if key.startswith('sec_iapd:'):
            return 'https://adviserinfo.sec.gov/'
        if key.startswith('sec_fulltext:'):
            return 'https://efts.sec.gov/LATEST/search-index?q=' + quote_plus(key[13:])
        if key.startswith('sec_submissions:'):
            return 'https://data.sec.gov/submissions/CIK' + key[16:].rjust(10, '0') + '.json'
        if key.startswith('sec_financials:'):
            return 'https://data.sec.gov/api/xbrl/companyfacts/CIK' + key.split(':')[1].rjust(10, '0') + '.json'
        if key.startswith('yahoo_finance:'):
            return 'https://finance.yahoo.com/quote/' + key.split(':')[1] + '/'
        if key == 'google_search':
            return 'https://www.google.com/'
        if key == 'linkedin':
            return 'https://www.linkedin.com/'
        if key.startswith('sec_filing:'):
            return 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=' + key.split(':')[1]
        if key == 'bizapedia':
            return 'https://www.bizapedia.com/'
        if key.startswith('trademark:'):
            return 'https://www.bizapedia.com/ (trademark search)'
        if key.startswith('northdata:'):
            return 'https://www.northdata.com/'
        if key == 'northdata_network':
            return 'https://www.northdata.com/ (ownership network)'
        if key == 'ownership_chain':
            return 'https://find-and-update.company-information.service.gov.uk/ (PSC chain)'
        if key.startswith('edgar_parent:'):
            return 'https://efts.sec.gov/ (Exhibit 21 search)'
        if key == 'ch_corporate_appointments':
            return 'https://find-and-update.company-information.service.gov.uk/ (corporate officer appointments)'
        if key.startswith('ch_brand_search:'):
            return 'https://find-and-update.company-information.service.gov.uk/ (brand name search)'
        if key == 'sec_cross_reference':
            return 'cross-reference of SEC data against website content'
        return key


def _unique(seq):
    """PHP array_unique preserving first-occurrence order (values re-indexed as a list)."""
    return list(dict.fromkeys(seq))
