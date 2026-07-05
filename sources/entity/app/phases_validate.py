"""
Entity Lookup v3b (Python) — Phase 7/8: registry validation + re-analysis.

Faithful port of php/lookup.php:
    validateEntityInRegistry        -> validate_entity_in_registry
    validateSingleEntity            -> validate_single_entity
    reanalyzeAfterValidationFailure -> reanalyze_after_validation_failure

Mixed into EntityLookup via multiple inheritance. Calls `self` for the LLM layer,
logging, config, and tools (self.tools.<snake_case>). Two methods live in other
mixins and are called through self:
    self.format_evidence(...)            (analysis mixin — PHP formatEvidence)
    self.search_registries_for_name(...) (RegistrySearchMixin — PHP searchRegistriesForName)
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote_plus, urlencode


class ValidationMixin:
    # ── Phase 7: validateEntityInRegistry (~1956) ───────────────────────────
    def validate_entity_in_registry(self, report: dict) -> dict:
        entity = report.get('recommended_entity') or None
        if not entity or not entity.get('registry_id'):
            return report

        registry_id = entity['registry_id']
        country = (entity.get('jurisdiction_country') or '').upper()
        state = (entity.get('jurisdiction_state') or '').upper()
        llm_name = entity.get('legal_entity_name') or ''

        self.log('validate', f"Validating \"{llm_name}\" — registry_id: {registry_id}, country: {country}, state: {state}")

        registry_name = None
        registry_status = None
        registry_data = None
        source = None
        validation_url = None

        # US → Bizapedia by file number + state
        if country == 'US' and state:
            self.log('validate', f"Looking up Bizapedia: file number {registry_id} in {state}...")
            biz = self.tools.lookup_bizapedia_by_file_number(registry_id, state)
            if biz:
                registry_name = biz.get('EntityName')
                registry_status = biz.get('FilingStatus')
                registry_data = biz
                source = 'Bizapedia'
                validation_url = '/validate.php?' + urlencode({
                    'entity_name': llm_name, 'registry_id': registry_id, 'country': 'US', 'state': state})
                entity_type = (biz.get('EntityType') or '').upper()
                domestic_state = biz.get('DomesticJurisdictionPostalAbbreviation')
                # Check for branch (Foreign) registration
                if 'FOREIGN' in entity_type or 'OUT OF STATE' in entity_type:
                    registry_status = f"Branch ({state}) — home: {domestic_state}"
                # Check for fictitious name (trade name, not a legal entity)
                if 'FICTITIOUS' in entity_type:
                    fictitious_owner = None
                    for p in (biz.get('Principals') or []):
                        if (p.get('Titles') or '').lower() == 'owner' and p.get('PrincipalName'):
                            fictitious_owner = p['PrincipalName']
                            break
                    owner_note = f" (owner: {fictitious_owner})" if fictitious_owner else ''
                    registry_status = f"Fictitious name{owner_note}"
                self.log('validate', f"Bizapedia returned: \"{registry_name}\" (status: {registry_status})", {
                    'expandable': True,
                    'sections': [{'label': 'Full Bizapedia Record',
                                  'content': json.dumps(biz, indent=4)}],
                })
            else:
                self.log('validate', f"Bizapedia: no result for file number {registry_id} in {state}")

                # Fallback: Delaware Division of Corporations for DE entities
                if state == 'DE':
                    self.log('validate', f"Trying Delaware Div. of Corps: file number {registry_id}...")
                    de = self.tools.lookup_delaware_by_file_number(registry_id)
                    if de:
                        registry_name = de['name']
                        de_status = (de.get('status') or '').lower()
                        # Map Delaware statuses to our status format
                        registry_status = 'Active' if 'good standing' in de_status else (de.get('status') or 'unknown')
                        registry_data = de
                        source = 'Delaware Div. of Corps.'
                        validation_url = 'https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx'
                        self.log('validate', f"Delaware returned: \"{registry_name}\" (status: {registry_status})", {
                            'expandable': True,
                            'sections': [{'label': 'Delaware Record',
                                          'content': json.dumps(de, indent=4)}],
                        })
                    else:
                        self.log('validate', f"Delaware: no result for file number {registry_id}")

        # UK → Companies House by company number
        if country == 'GB':
            self.log('validate', f"Looking up Companies House: company number {registry_id}...")
            ch = self.tools.lookup_companies_house_by_number(registry_id)
            if ch:
                registry_name = ch.get('company_name')
                registry_status = ch.get('company_status')
                registry_data = ch
                source = 'Companies House'
                validation_url = f"https://find-and-update.company-information.service.gov.uk/company/{registry_id}"
                self.log('validate', f"Companies House returned: \"{registry_name}\" (status: {registry_status})", {
                    'expandable': True,
                    'sections': [{'label': 'Full CH Record',
                                  'content': json.dumps(ch, indent=4)}],
                })
            else:
                self.log('validate', f"Companies House: no result for company number {registry_id}")

        # Europe → NorthData: search by name, verify country + registry ID
        northdata_countries = ['DE', 'NL', 'FR', 'AT', 'CH', 'BE', 'LU', 'IT', 'ES', 'DK', 'SE', 'NO', 'FI', 'PL', 'CZ', 'IE']
        if country in northdata_countries and not registry_name:
            self.log('validate', f"Searching NorthData for \"{llm_name}\" (country: {country}, registry ID: {registry_id})...")
            nd = self.tools.validate_northdata_entity(llm_name, registry_id, country)
            if nd:
                full_nd_name = re.sub(r'\s*\([^)]*\)\s*$', '', nd['name'])  # strip (liq) etc
                parts = [p.strip() for p in full_nd_name.split(',')]
                registry_name = ', '.join(parts[:-2]) if len(parts) >= 3 else parts[0]
                registry_status = nd.get('status') or 'unknown'
                registry_data = nd
                source = 'NorthData'
                validation_url = nd.get('url') or ("https://www.northdata.com/" + quote_plus(llm_name))
                country_match = nd.get('country_match') or False
                reg_id_match = nd.get('registry_id_match') or False

                country_note = "country confirmed" if country_match else "country NOT confirmed"
                reg_id_note = (f"registry ID \"{registry_id}\" found on page" if reg_id_match
                               else f"registry ID \"{registry_id}\" not found on page")

                self.log('validate', f"NorthData: name: \"{registry_name}\", status: {registry_status}, {country_note}, {reg_id_note}", {
                    'expandable': True,
                    'sections': [{'label': 'NorthData Result',
                                  'content': json.dumps(nd, indent=4)}],
                })

                # Primary: entity must exist with correct country
                if not country_match:
                    registry_name = None
                    self.log('validate', f"Discarding result — country mismatch for \"{llm_name}\"")
            else:
                self.log('validate', f"NorthData: no results for \"{llm_name}\"")

        # Build validation result
        if not registry_name:
            report['registry_validation'] = {
                'status': 'not_found',
                'message': f"Registry ID {registry_id} not found in {source or 'registry'}",
            }
            self.log('validate', "VALIDATION FAILED — entity not found in registry")
            return report

        # Compare names (case-insensitive, normalise punctuation)
        norm_llm = re.sub(r'[^A-Z0-9 ]', '', llm_name.upper()).upper()
        norm_reg = re.sub(r'[^A-Z0-9 ]', '', (registry_name or '').upper()).upper()
        name_match = norm_llm == norm_reg

        # Check status
        status_lower = (registry_status or '').lower()
        status_ok = status_lower in ['active', 'unknown']

        if name_match and status_ok:
            report['registry_validation'] = {
                'status': 'verified',
                'message': f"Verified: \"{registry_name}\" is {registry_status} in {source}",
                'registry_name': registry_name,
                'registry_status': registry_status,
                'source': source,
                'validation_url': validation_url,
            }
            self.log('validate', f"VERIFIED — \"{registry_name}\" matches, status: {registry_status}")
        elif name_match and not status_ok:
            report['registry_validation'] = {
                'status': 'name_match_bad_status',
                'message': f"Name matches but status is \"{registry_status}\" (not active) in {source}",
                'registry_name': registry_name,
                'registry_status': registry_status,
                'source': source,
                'validation_url': validation_url,
            }
            self.log('validate', f"WARNING — name matches but status is \"{registry_status}\"")
            # Downgrade confidence if entity is dissolved/canceled
            if (report.get('confidence') or '') in ['high', 'medium']:
                report['confidence'] = 'low'
                report['note'] = (report.get('note') or '') + f" [Registry validation: entity status is \"{registry_status}\" — confidence downgraded.]"
                self.log('validate', "Confidence downgraded to LOW due to inactive status")
        else:
            report['registry_validation'] = {
                'status': 'name_mismatch',
                'message': f"Name mismatch: LLM said \"{llm_name}\" but registry has \"{registry_name}\"",
                'registry_name': registry_name,
                'registry_status': registry_status,
                'source': source,
                'validation_url': validation_url,
            }
            self.log('validate', f"WARNING — name mismatch: expected \"{llm_name}\", registry has \"{registry_name}\"")
            if (report.get('confidence') or '') in ['high', 'medium']:
                report['confidence'] = 'low'
                report['note'] = (report.get('note') or '') + f" [Registry validation: name mismatch — LLM returned \"{llm_name}\" but registry has \"{registry_name}\".]"
                self.log('validate', "Confidence downgraded to LOW due to name mismatch")

        return report

    # ── validateSingleEntity (~2156) ────────────────────────────────────────
    def validate_single_entity(self, name: str, registry_id: str, country: str, state: str | None = None) -> dict:
        registry_name = None
        registry_status = None
        source = None
        validation_url = None

        # US → Bizapedia, then Delaware fallback
        if country == 'US' and state:
            biz = self.tools.lookup_bizapedia_by_file_number(registry_id, state)
            if biz:
                registry_name = biz.get('EntityName')
                registry_status = biz.get('FilingStatus')
                source = 'Bizapedia'
                validation_url = '/validate.php?' + urlencode({
                    'entity_name': name, 'registry_id': registry_id, 'country': 'US', 'state': state})
                entity_type = (biz.get('EntityType') or '').upper()
                if 'FOREIGN' in entity_type or 'OUT OF STATE' in entity_type:
                    registry_status = 'Branch'
                if 'FICTITIOUS' in entity_type:
                    registry_status = 'Fictitious name'
            elif state == 'DE':
                de = self.tools.lookup_delaware_by_file_number(registry_id)
                if de:
                    registry_name = de['name']
                    registry_status = 'Active'
                    source = 'Delaware Div. of Corps.'
                    validation_url = 'https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx'

        # UK → Companies House
        if country == 'GB' and not registry_name:
            ch = self.tools.lookup_companies_house_by_number(registry_id)
            if ch:
                registry_name = ch.get('company_name')
                registry_status = ch.get('company_status')
                source = 'Companies House'
                validation_url = f"https://find-and-update.company-information.service.gov.uk/company/{registry_id}"

        # EU → NorthData
        nd_countries = ['DE', 'NL', 'FR', 'AT', 'CH', 'BE', 'LU', 'IT', 'ES', 'DK', 'SE', 'NO', 'FI', 'PL', 'CZ', 'IE']
        if country in nd_countries and not registry_name:
            nd = self.tools.validate_northdata_entity(name, registry_id, country)
            if nd and (nd.get('country_match') or False):
                full_nd_name = re.sub(r'\s*\([^)]*\)\s*$', '', nd['name'])
                parts = [p.strip() for p in full_nd_name.split(',')]
                registry_name = ', '.join(parts[:-2]) if len(parts) >= 3 else parts[0]
                registry_status = nd.get('status') or 'unknown'
                source = 'NorthData'
                validation_url = nd.get('url')

        if not registry_name:
            return {'status': 'not_found', 'source': source}

        norm_llm = re.sub(r'[^A-Z0-9 ]', '', name.upper()).upper()
        norm_reg = re.sub(r'[^A-Z0-9 ]', '', (registry_name or '').upper()).upper()
        name_match = norm_llm == norm_reg
        status_lower = (registry_status or '').lower()
        status_ok = status_lower in ['active', 'unknown']

        if name_match and status_ok:
            return {'status': 'verified', 'registry_name': registry_name, 'source': source, 'validation_url': validation_url}
        elif name_match:
            return {'status': 'inactive', 'registry_name': registry_name, 'registry_status': registry_status, 'source': source, 'validation_url': validation_url}
        else:
            return {'status': 'name_mismatch', 'registry_name': registry_name, 'source': source, 'validation_url': validation_url}

    # ── Phase 8: reanalyzeAfterValidationFailure (~2238) ────────────────────
    def reanalyze_after_validation_failure(self, report: dict, url: str, domain: str,
                                           website_data: dict, entity_info: dict, registries: dict) -> dict:
        rv = report.get('registry_validation') or {}
        rv_status = rv.get('status') or ''
        registry_name = rv.get('registry_name')
        llm_name = ((report.get('recommended_entity') or {}).get('legal_entity_name'))
        original_names = [n.lower() for n in (entity_info.get('entity_names') or [])]

        # Save original report
        original_report = report

        # Collect names to search: the LLM's recommended name + the registry-returned name
        names_to_search = []
        if llm_name:
            names_to_search.append(llm_name)
        if registry_name:
            names_to_search.append(registry_name)

        # For each name to search: run new searches if not done in Phase 3,
        # otherwise include the existing Phase 3 Bizapedia results for that name
        supplementary_results: dict = {}
        searched_norms: dict = {}
        for name in names_to_search:
            norm_name = re.sub(r'[^a-zA-Z0-9 ]', '', name.lower()).strip().lower()
            if norm_name in searched_norms:
                continue
            searched_norms[norm_name] = True

            already_searched = False
            for on in original_names:
                norm_on = re.sub(r'[^a-zA-Z0-9 ]', '', on).strip().lower()
                if norm_on == norm_name:
                    already_searched = True
                    break

            if not already_searched:
                self.log('reanalysis', f"\"{name}\" was not searched in Phase 3 — running supplementary searches")
                new_results = self.search_registries_for_name(name, entity_info, registries)
                supplementary_results.update(new_results)
            else:
                self.log('reanalysis', f"\"{name}\" was already searched in Phase 3 — including existing Bizapedia results")
                biz_key = f"bizapedia:{name}"
                if biz_key in registries:
                    supplementary_results[biz_key] = registries[biz_key]

        # Build the follow-up prompt
        original_report_json = json.dumps(report, indent=4)
        source = rv.get('source') or 'the registry'
        registry_status = rv.get('registry_status')

        # Lead with "could not be verified", then add context as notes
        validation_summary = f"Your recommended entity \"{llm_name}\" could not be verified in the registry.\n"

        # Supplementary searches headline
        searched_names: dict = {}
        for key, result in supplementary_results.items():
            m = re.match(r'^[^:]+:(.+)$', key)
            if m:
                searched_names[m.group(1).strip()] = True
        if searched_names:
            name_list = '" and "'.join(searched_names.keys())
            validation_summary += f"We searched {source} for \"{name_list}\" and found the supplementary results below.\n"

        # Context notes (secondary)
        registry_id = ((report.get('recommended_entity') or {}).get('registry_id'))
        if rv_status == 'name_mismatch' and registry_name and registry_id:
            if registry_status and 'fictitious' in registry_status.lower():
                validation_summary += f"Note: the registry_id {registry_id} you provided is a fictitious name (trade name) registration for \"{registry_name}\", not a legal entity.\n"
            else:
                validation_summary += f"Note: the registry_id {registry_id} you provided belongs to \"{registry_name}\", not \"{llm_name}\".\n"
        elif rv_status == 'not_found' and registry_id:
            validation_summary += f"Note: registry_id {registry_id} was not found in {source}.\n"
        elif rv_status == 'name_match_bad_status':
            validation_summary += f"Note: \"{registry_name}\" was found but its status is \"{registry_status}\" (not active).\n"
        elif not registry_id:
            validation_summary += "Note: no registry_id was provided. The supplementary results below may help identify the correct filing.\n"
        if rv.get('is_branch'):
            validation_summary += f"Note: this is a branch (foreign) registration. Domestic jurisdiction: {rv.get('domestic_state') or 'unknown'}.\n"

        supplementary_text = ''
        if supplementary_results:
            supplementary_text = "\n\n=== SUPPLEMENTARY REGISTRY RESULTS ===\n"
            for key, result in supplementary_results.items():
                truncated = result[:10000]
                supplementary_text += f"\n--- {key} ---\n{truncated}\n"

        system_prompt = self.analysis_prompt + "\n\n" + self.json_schema
        user_message = (
            f"Your previous analysis for {url} was checked against the official registry and could not be verified.\n"
            "\n"
            "=== WHAT WENT WRONG ===\n"
            f"{validation_summary}"
            "=== YOUR PREVIOUS REPORT ===\n"
            f"{original_report_json}\n"
            f"{supplementary_text}\n"
            "\n"
            "=== ORIGINAL EVIDENCE ===\n"
            f"{self.format_evidence(url, domain, website_data, entity_info, registries)}\n"
            "\n"
            "Please re-analyze using the new registry data and produce a corrected report in the same JSON format."
        )

        # Log the re-analysis prompt
        self.log('reanalysis', "Calling LLM for re-analysis with validation failure context", {
            'expandable': True,
            'sections': [
                {'label': 'Validation Summary', 'content': validation_summary},
                {'label': 'Supplementary Results', 'content': supplementary_text or '(none — name already searched)'},
            ],
        })

        input_tokens_before = self.total_input_tokens
        output_tokens_before = self.total_output_tokens

        response_text = self.call_llm(system_prompt, user_message, 8192)

        reanalysis_cost = ((self.total_input_tokens - input_tokens_before) * 3.0 / 1_000_000) \
            + ((self.total_output_tokens - output_tokens_before) * 15.0 / 1_000_000)

        self.log('reanalysis', f"Re-analysis response ({len(response_text):,} chars, ${reanalysis_cost:,.4f})", {
            'expandable': True,
            'sections': [{'label': 'Output', 'content': response_text}],
        })

        new_report = self.parse_json_response(response_text, original_report)

        # Re-validate the new recommendation
        new_entity = new_report.get('recommended_entity') or None
        if new_entity and new_entity.get('registry_id'):
            self.log('reanalysis', f"Re-validating new recommendation: \"{new_entity['legal_entity_name']}\" ({new_entity['registry_id']})")
            new_report = self.validate_entity_in_registry(new_report)
            new_rv_status = (new_report.get('registry_validation') or {}).get('status') or 'unknown'
            self.log('reanalysis', f"Re-validation result: {new_rv_status}")

        # Store original report for comparison
        new_report['original_report'] = original_report

        return new_report
