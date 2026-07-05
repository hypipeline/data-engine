"""
Entity Lookup v3b (Python) — EVIDENCE-CHAIN + FINAL-ANALYSIS phases.

Faithful, like-for-like port of the analysis methods of php/lookup.php
(class EntityLookup):

    PHP method                 →  Python method
    ────────────────────────────────────────────────────
    crossReferenceSecData      →  cross_reference_sec_data
    summarizeResult            →  summarize_result
    logRegistryResult          →  log_registry_result
    analyzeEvidence            →  analyze_evidence
    formatEvidence             →  format_evidence

Exact prompt assembly, evidence formatting, SEC-vs-website cross-reference logic,
truncation and every self.log(...) message are preserved verbatim. This mixin is
combined into EntityLookup via multiple inheritance; every `self.*` used here
(config, log, analysis_prompt, json_schema, call_llm, parse_json_response,
scrub_blocked_names, match_entity_name, registry_key_to_source) is provided by
agent.py's EntityLookup and the sibling mixins.

Cross-mixin calls (methods NOT in this port group — provided elsewhere):
    self.scrub_blocked_names(...)     — ExtractionMixin (phases_extract.py)
    self.match_entity_name(...)       — RegistrySearchMixin (phases_registry.py)
    self.registry_key_to_source(...)  — RegistrySearchMixin (phases_registry.py)

stdlib only (re + json + datetime).
"""
from __future__ import annotations

import datetime
import json
import re


# ── PHP semantics helpers ───────────────────────────────────────────────────

def _php_json_encode(data) -> str:
    """Mirror PHP json_encode() default behaviour: no spaces, \\uXXXX for
    non-ASCII, and '/' escaped as '\\/'. A dict becomes a JSON object, a list a
    JSON array (matching PHP's list-vs-object distinction for arrays)."""
    s = json.dumps(data, separators=(',', ':'), ensure_ascii=True)
    return s.replace('/', '\\/')


def _php_truthy(v) -> bool:
    """PHP truthiness: false, 0, 0.0, '', '0', null, [] are falsy."""
    if v is None or v is False:
        return False
    if v is True:
        return True
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v != '' and v != '0'
    if isinstance(v, (list, dict, tuple, set)):
        return len(v) > 0
    return bool(v)


def _php_trim(s) -> str:
    """PHP trim() default character set."""
    if not isinstance(s, str):
        s = str(s)
    return s.strip(" \t\n\r\0\x0b")


def _php_strtolower(s: str) -> str:
    """PHP strtolower(): ASCII A-Z only (leaves multibyte chars untouched)."""
    return ''.join(chr(ord(c) + 32) if 'A' <= c <= 'Z' else c for c in s)


def _php_strtok_nl(s: str) -> str:
    """PHP strtok($s, "\\n"): first token, skipping any leading newlines.
    Returns '' when there is no token (PHP strtok would return false)."""
    stripped = s.lstrip("\n")
    if stripped == '':
        return ''
    return stripped.split("\n", 1)[0]


def _isset(d, key, default):
    """PHP $d[$key] ?? $default (missing OR null → default)."""
    v = d.get(key)
    return v if v is not None else default


def _coalesce(d, key1, key2, default):
    """PHP $d[$key1] ?? $d[$key2] ?? $default."""
    v = d.get(key1)
    if v is not None:
        return v
    v = d.get(key2)
    if v is not None:
        return v
    return default


def _ucfirst(s: str) -> str:
    """PHP ucfirst(): uppercase first character only (rest untouched)."""
    if not s:
        return s
    return s[0].upper() + s[1:]


class EvidenceAnalysisMixin:

    # ── Cross-reference SEC data against website ────────────────────────────

    def cross_reference_sec_data(self, website_data, registries, entity_info):
        # PHP crossReferenceSecData(array $websiteData, array $registries, array $entityInfo): string

        # Collect all website text for matching
        website_text = _php_strtolower("\n".join(website_data['pages'].values()))
        if len(website_text) < 100:
            self.log('crossref', "Skipping cross-reference — website text too short (" + str(len(website_text)) + " chars)")
            return ''

        # Website addresses from LLM extraction (must be real street addresses)
        raw_addresses = _coalesce(entity_info, 'addresses', 'address', [])
        if isinstance(raw_addresses, str):
            raw_addresses = [raw_addresses] if _php_truthy(raw_addresses) else []
        # array_filter(array_map('trim', ...)) — trim then drop falsy, preserving keys
        _mapped = [_php_trim(x) for x in raw_addresses]
        _addr_items = [(i, v) for i, v in enumerate(_mapped) if _php_truthy(v)]
        website_addresses = [v for _, v in _addr_items]
        # PHP json_encode of a filtered array: JSON array if keys are still 0..n-1,
        # otherwise a JSON object keyed by the preserved original indices.
        if [i for i, _ in _addr_items] == list(range(len(_addr_items))):
            _addr_json_val = website_addresses
        else:
            _addr_json_val = {str(i): v for i, v in _addr_items}

        self.log('crossref', "Website addresses from LLM: " + (_php_json_encode(_addr_json_val) if website_addresses else "(none extracted)"))

        # Count how many SEC submissions we have to work with
        sec_keys = [k for k in registries.keys() if k.startswith('sec_submissions:')]
        self.log('crossref', "Found " + str(len(sec_keys)) + " SEC submission record(s) to cross-reference: " + ', '.join(sec_keys))

        matches = []

        # Extract SEC submission data for cross-referencing
        for key, val in registries.items():
            if not key.startswith('sec_submissions:'):
                continue
            try:
                sub = json.loads(val)
            except Exception:
                sub = None
            if not sub or not isinstance(sub, dict) or not _php_truthy(sub.get('name')):
                self.log('crossref', f"  {key}: skipped — no parseable data")
                continue

            cik = _isset(sub, 'cik', '')
            entity_name = _isset(sub, 'name', '')
            edgar_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"

            self.log('crossref', f"  Checking SEC entity: \"{entity_name}\" (CIK {cik})")

            # 1. Entity name on website
            name_match = self.match_entity_name(entity_name, website_text)
            if name_match:
                label = '' if name_match == entity_name else f" (normalised from \"{entity_name}\")"
                matches.append(f"ENTITY NAME MATCH: \"{name_match}\"{label} found on website | {edgar_url}")
                self.log('crossref', f"    ✓ Name match: \"{name_match}\" found on website")
            else:
                self.log('crossref', f"    ✗ Name \"{entity_name}\" not found on website")

            # 2. Address matching
            _addr = sub.get('addresses')
            biz_addr = _addr.get('business') if isinstance(_addr, dict) else None
            if not isinstance(biz_addr, dict):
                biz_addr = {}
            sec_city = _isset(biz_addr, 'city', '')
            sec_state = _coalesce(biz_addr, 'stateOrCountryDescription', 'stateOrCountry', '')
            sec_street = _php_trim(str(_isset(biz_addr, 'street1', '')) + ' ' + str(_isset(biz_addr, 'street2', '')))
            sec_zip = _isset(biz_addr, 'zipCode', '')
            sec_addr_full = ', '.join([p for p in [sec_street, sec_city, sec_state, sec_zip] if _php_truthy(p)])

            self.log('crossref', f"    SEC address: \"{sec_addr_full}\"")

            if website_addresses:
                # Extract street name words from SEC address (skip house number)
                sec_street_clean = _php_strtolower(re.sub(r'[.,]', '', sec_street))
                street_words = re.split(r'\s+', sec_street_clean)
                street_name = ''
                for i, w in enumerate(street_words):
                    if i == 0 and re.match(r'\d', w):
                        continue
                    if w in ['c/o', 'suite', 'ste', 'floor', 'flr', 'box', 'apt', 'unit']:
                        break
                    if len(w) > 2:
                        street_name += (' ' if street_name else '') + w
                self.log('crossref', f"    Extracted street name: \"{street_name}\" — comparing against " + str(len(website_addresses)) + " website address(es)")

                matched = False
                for wa in website_addresses:
                    wa_clean = _php_strtolower(re.sub(r'[.,]', '', wa))

                    if street_name and len(street_name) > 4 and street_name in wa_clean:
                        matches.append(f"ADDRESS MATCH (street): \"{sec_street}\" matches website address \"{wa}\" | SEC: {sec_addr_full} | {edgar_url}")
                        self.log('crossref', f"    ✓ Street match: \"{street_name}\" found in website address \"{wa}\"")
                        matched = True
                        break
                    elif sec_zip and _php_strtolower(str(sec_zip)) in wa_clean:
                        matches.append(f"ADDRESS MATCH (zip): ZIP \"{sec_zip}\" matches website address \"{wa}\" | SEC: {sec_addr_full} | {edgar_url}")
                        self.log('crossref', f"    ✓ ZIP match: \"{sec_zip}\" found in website address \"{wa}\"")
                        matched = True
                        break
                if not matched and sec_city:
                    generic_cities = ['new york', 'london', 'los angeles', 'chicago', 'houston', 'phoenix', 'san antonio', 'san diego', 'dallas', 'austin']
                    if _php_strtolower(str(sec_city)) in generic_cities:
                        self.log('crossref', f"    ✗ City \"{sec_city}\" is too generic for city-only match")
                    else:
                        city_matched = False
                        for wa in website_addresses:
                            wa_clean = _php_strtolower(re.sub(r'[.,]', '', wa))
                            if _php_strtolower(str(sec_city)) in wa_clean:
                                matches.append(f"ADDRESS MATCH (city): City \"{sec_city}\" in website address \"{wa}\" | SEC: {sec_addr_full} | {edgar_url}")
                                self.log('crossref', f"    ✓ City match: \"{sec_city}\" found in website address \"{wa}\"")
                                city_matched = True
                                break
                        if not city_matched:
                            self.log('crossref', "    ✗ No address match (street/zip/city)")
                elif not matched:
                    self.log('crossref', "    ✗ No address match (street/zip)")
            else:
                self.log('crossref', "    — No website addresses to compare against")

            # 3. Phone matching
            sec_phone = _isset(sub, 'phone', '')
            if _php_truthy(sec_phone):
                phone_digits = re.sub(r'\D', '', str(sec_phone))
                if len(phone_digits) >= 10 and phone_digits in re.sub(r'\D', '', website_text):
                    matches.append(f"PHONE MATCH: {sec_phone} found on website | {edgar_url}")
                    self.log('crossref', f"    ✓ Phone match: {sec_phone}")
                else:
                    self.log('crossref', f"    ✗ Phone \"{sec_phone}\" not found on website")
            else:
                self.log('crossref', "    — No phone number in SEC data")

            # 4. Related persons and GP entities from Form D
            form_d_key = f"sec_filing:{cik}:FormD"
            form_d_content = registries.get(form_d_key, '')
            if _php_truthy(form_d_content):
                form_d_lines = form_d_content.split("\n")
                person_names = []
                gp_entities = []

                n = len(form_d_lines)
                for i in range(n):
                    line = _php_trim(form_d_lines[i])

                    if line == 'Last Name' and i + 2 < n:
                        last_name = ''
                        first_name = ''
                        for j in range(i + 1, min(i + 8, n)):
                            val2 = _php_trim(form_d_lines[j])
                            if val2 == 'First Name' or val2 == 'Middle Name' or val2 == 'Last Name':
                                continue
                            if not last_name:
                                last_name = val2
                                continue
                            if not first_name:
                                first_name = val2
                                break
                        if first_name and first_name != 'n/a' and last_name and last_name != 'n/a':
                            person_names.append(f"{first_name} {last_name}")
                        elif last_name and first_name == 'n/a':
                            gp_entities.append(last_name)

                    if line == 'Name of Signer' and i + 1 < n:
                        signer = _php_trim(form_d_lines[i + 1])
                        if re.match(r'^[A-Z][a-z]+\s+[A-Z][a-z]+$', signer):
                            person_names.append(signer)

                # array_unique — dedupe preserving first-seen order
                _seen = set()
                _uniq = []
                for p in person_names:
                    if p not in _seen:
                        _seen.add(p)
                        _uniq.append(p)
                person_names = _uniq
                self.log('crossref', "    Form D persons: " + (_php_json_encode(person_names) if person_names else "(none)") + " | GP entities: " + (_php_json_encode(gp_entities) if gp_entities else "(none)"))

                for person in person_names:
                    if len(person) > 4 and _php_strtolower(person) in website_text:
                        matches.append(f"PERSON MATCH: \"{person}\" (Related Person in Form D) found on website | {edgar_url}")
                        self.log('crossref', f"    ✓ Person match: \"{person}\" found on website")
                    else:
                        self.log('crossref', f"    ✗ Person \"{person}\" not found on website")

                for gp in gp_entities:
                    gp_match = self.match_entity_name(gp, website_text)
                    if gp_match:
                        label = '' if gp_match == _php_strtolower(gp) else f" (normalised from \"{gp}\")"
                        matches.append(f"GP ENTITY MATCH: \"{gp_match}\"{label} (General Partner in Form D) found on website | {edgar_url}")
                        self.log('crossref', f"    ✓ GP entity match: \"{gp}\" found on website")
                    else:
                        self.log('crossref', f"    ✗ GP entity \"{gp}\" not found on website")
            else:
                self.log('crossref', f"    — No Form D filing found for CIK {cik}")

        # Deduplicate
        _seen = set()
        _uniq = []
        for m in matches:
            if m not in _seen:
                _seen.add(m)
                _uniq.append(m)
        matches = _uniq

        if not matches:
            self.log('crossref', "No SEC-to-website cross-references found")
            return ''

        result = "=== SEC CROSS-REFERENCE EVIDENCE ===\n"
        if website_addresses:
            result += "WEBSITE ADDRESSES (from LLM extraction): " + ' | '.join(website_addresses) + "\n"
        result += "\n".join(matches)

        self.log('crossref', str(len(matches)) + " SEC-to-website cross-reference(s) found", {
            'expandable': True,
            'sections': [{'label': 'Cross-reference Detail', 'content': result}],
        })

        return result

    # ── Phase 4: LLM Analysis ────────────────────────────────────────────────

    def summarize_result(self, result):
        # PHP summarizeResult(string $result): string
        first_line = _php_strtok_nl(result)
        if len(first_line) > 80:
            first_line = first_line[:80] + '...'
        line_count = result.count("\n") + 1
        return f"{first_line} (+{line_count} lines)" if line_count > 1 else first_line

    def log_registry_result(self, phase, source, name, result, entity_name=None):
        # PHP logRegistryResult(string $phase, string $source, string $name, string $result, ?string $entityName = null): void
        summary = self.summarize_result(result)
        line_count = result.count("\n") + 1
        detail = {
            'expandable': True,
            'sections': [{'label': f"Full Result ({line_count} lines)", 'content': result}],
        } if line_count > 1 else {}
        if entity_name:
            detail['entity_name'] = entity_name
        self.log(phase, f"{source}: \"{name}\" → {summary}", detail if detail else None)

    def analyze_evidence(self, url, domain, website_data, entity_info, registries):
        # PHP analyzeEvidence(string $url, string $domain, array $websiteData, array $entityInfo, array $registries): array
        evidence_text = self.format_evidence(url, domain, website_data, entity_info, registries)
        system_prompt = self.analysis_prompt + "\n\n" + self.json_schema
        user_message = f"Analyze the following evidence and produce the entity lookup report:\n\n{evidence_text}"

        # Build expandable sections for the evidence
        evidence_sections = [
            {'label': 'System Prompt', 'content': system_prompt},
            {'label': 'Metadata', 'content': "TARGET URL: " + url + "\nDOMAIN: " + domain
                + "\nCANDIDATE ENTITY NAMES: " + _php_json_encode(_isset(entity_info, 'entity_names', []))
                + "\nJURISDICTION: " + _php_json_encode([_isset(entity_info, 'jurisdiction', 'unknown')])
                + "\nADDRESSES: " + _php_json_encode(_coalesce(entity_info, 'addresses', 'address', []))},
            {'label': 'WHOIS', 'content': self.scrub_blocked_names(_isset(website_data, 'whois', 'Not available'))},
        ]
        for page_name, text in website_data['pages'].items():
            truncated = text[:4000]
            evidence_sections.append({'label': "Website: " + _ucfirst(page_name) + " (" + f"{len(truncated):,}" + " chars)", 'content': truncated})
        for key, result in registries.items():
            truncated = result[:10000]
            evidence_sections.append({'label': f"Registry: {key} (" + f"{len(truncated):,}" + " chars)", 'content': truncated})
        self.log('llm', "LLM analysis — calling " + self.config['model'] + " with " + str(len(registries)) + " registry results (" + f"{len(evidence_text):,}" + " chars)", {
            'expandable': True,
            'sections': evidence_sections,
        })

        response_text = self.call_llm(system_prompt, user_message, 8192)

        self.log('llm', "LLM analysis response (" + f"{len(response_text):,}" + " chars)", {
            'expandable': True,
            'sections': [
                {'label': 'Output', 'content': response_text},
            ],
        })

        report = self.parse_json_response(response_text, {
            'input_url': url,
            'date': datetime.date.today().strftime('%Y-%m-%d'),
            'report_id': 'ERROR',
            'recommended_entity': None,
            'confidence': 'insufficient',
            'note': 'LLM returned invalid JSON.',
            'evidence_forward': [],
            'evidence_reverse': [],
            'substance_score': 0,
            'substance_band': 'insufficient',
            'substance_factors': [],
            'corporate_structure': None,
            'key_people': [],
            'other_entities': [],
            'sources_used': [],
        })

        return report

    def format_evidence(self, url, domain, website_data, entity_info, registries):
        # PHP formatEvidence(string $url, string $domain, array $websiteData, array $entityInfo, array $registries): string
        page_urls = _isset(website_data, 'pageUrls', {})
        parts = []
        parts.append(f"TARGET URL: {url}")
        parts.append(f"DOMAIN: {domain}")
        parts.append("CANDIDATE ENTITY NAMES: " + _php_json_encode(_isset(entity_info, 'entity_names', [])))
        parts.append("LIKELY JURISDICTIONS: " + _php_json_encode([_isset(entity_info, 'jurisdiction', 'unknown')]))

        whois_source = f"whois lookup for {domain}"
        parts.append("\n=== WHOIS ===")
        parts.append(f"source: {whois_source}")
        parts.append(self.scrub_blocked_names(_isset(website_data, 'whois', 'Not available')))

        for page_name, text in website_data['pages'].items():
            truncated = text[:4000]
            source_url = page_urls.get(page_name) if isinstance(page_urls, dict) else None
            if source_url is None:
                source_url = url
            parts.append("\n=== WEBSITE: " + page_name.upper() + " ===")
            parts.append(f"source: {source_url}")
            parts.append(truncated)

        for key, result in registries.items():
            truncated = result[:10000]
            source = self.registry_key_to_source(key)
            parts.append(f"\n=== REGISTRY: {key} ===")
            parts.append(f"source: {source}")
            parts.append(truncated)

        return "\n".join(parts)
