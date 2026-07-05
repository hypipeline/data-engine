"""
Entity Lookup v3b (Python) — ENTITY-EXTRACTION phase.

Faithful, like-for-like port of the extraction methods of php/lookup.php
(class EntityLookup):

    PHP method                 →  Python method
    ────────────────────────────────────────────────────
    extractEntitiesWithLlm     →  extract_entities_with_llm
    extractCandidateNames      →  extract_candidate_names
    scrubBlockedNames          →  scrub_blocked_names
    deduplicateNames           →  deduplicate_names

Exact logic, prompt assembly, regexes, dedup, blocked-name scrubbing and log
messages are preserved. This mixin is combined into EntityLookup via multiple
inheritance; every `self.*` used here (config, log, extraction_prompt,
call_llm, parse_json_response, tools) is provided by agent.py's EntityLookup.

stdlib only (re + json via the host module).
"""
from __future__ import annotations

import re


class ExtractionMixin:

    # ── Regex Candidate Name Extraction ─────────────────────────────────────

    def scrub_blocked_names(self, text):
        # PHP scrubBlockedNames(string $text): string
        blocked = self.config.get('blocked_entity_names') or []
        if not blocked:
            return text

        lines = text.split("\n")
        scrubbed = []
        removed = 0
        for line in lines:
            skip = False
            for term in blocked:
                # PHP: stripos($line, $term) !== false  (case-insensitive substring)
                if str(term).lower() in line.lower():
                    skip = True
                    removed += 1
                    break
            if not skip:
                scrubbed.append(line)
        if removed:
            self.log('extract', f"Scrubbed {removed} WHOIS line(s) matching blocked names")
        return "\n".join(scrubbed)

    def extract_candidate_names(self, text, limit=5):
        # PHP extractCandidateNames(string $text, int $limit = 5): array

        # Unambiguous suffixes (case-insensitive, safe to match freely)
        # Note: S.A. requires negative lookbehind to avoid matching inside U.S.A.
        suffix_pattern = (
            r"(?:Ltd\.?|Limited|LLC|L\.L\.C\.?|Inc\.?|Incorporated|"
            r"Corp\.?|Corporation|LLP|L\.L\.P\.?|L\.P\.?|"
            r"PLC|Plc|p\.l\.c\.?|GmbH|mbH|KGaA|OHG|"
            r"S\.A\.S\.?|SAS|S\.A\.R\.L\.?|SARL|"
            r"B\.V\.?|BV|N\.V\.?|NV|C\.V\.?|CV|"
            r"S\.r\.l\.?|SRL|S\.p\.A\.?|SpA|"
            r"A\.S\.?|ApS|A\/S|Oyj|"
            r"(?<!\.)S\.A\.?|(?<!\.)S\.L\.?|(?<!\.)S\.C\.?|e\.V\.?|"
            r"Pty\.?\s*Ltd\.?|Co\.?\s*Ltd\.?)"
        )

        # Ambiguous suffixes — short uppercase words that are also common English words.
        # Matched separately: require preceding word to start with uppercase.
        ambiguous_pattern = (
            r"([A-Z][A-Za-z0-9\s&,'\.]+?)\s+"
            r"(A[Gg]|S[Aa]|S[Ee]|K[Gg]|U[Gg]|A[Ss]|A[Bb]|S[Ll]|S[Cc]|O[Yy]|LP|Company)\b"
        )

        candidates = {}

        # 1. Copyright notices — capture up to legal suffix only
        rx_copyright = re.compile(
            r"(?:©|\(c\)|copyright)\s*(?:\d{4}[–\-\s,]*\d{0,4}\s*)?(.+?" + suffix_pattern + r")\b",
            re.I,
        )
        for m in rx_copyright.finditer(text):
            name = m.group(1).strip(" .\t\n\r—–-")
            if 3 < len(name) < 120:
                candidates[name] = True
        # Copyright with ambiguous suffix (case-sensitive): "© 2024 adidas AG"
        rx_copyright_amb = re.compile(
            r"(?:©|\(c\)|copyright)\s*(?:\d{4}[–\-\s,]*\d{0,4}\s*)?"
            r"(.+?\s+(?:A[Gg]|S[Aa]|S[Ee]|K[Gg]|U[Gg]|A[Ss]|A[Bb]|S[Ll]|S[Cc]|O[Yy]|LP))\b"
        )
        for m in rx_copyright_amb.finditer(text):
            name = m.group(1).strip(" .\t\n\r—–-")
            if 3 < len(name) < 120:
                candidates[name] = True

        # 2. Lines containing legal suffixes
        rx_suffix_word = re.compile(r"\b" + suffix_pattern + r"\b", re.I)
        rx_ambiguous = re.compile(ambiguous_pattern)
        rx_operated = re.compile(
            r"(?:operated|owned|run|managed|maintained)\s+by\s+(.+?" + suffix_pattern + r")\b",
            re.I,
        )
        rx_entity_suffix = re.compile(
            r"\b((?:[A-Z][A-Za-z0-9'\.]*(?:\s+(?:&\s+)?|\s*,\s*)){0,5}"
            r"[A-Z][A-Za-z0-9'\.]*[\s,]+(?i:" + suffix_pattern + r"))\.?\b"
        )
        for line in text.split("\n"):
            line = line.strip()
            if not line or len(line) > 300:
                continue

            has_suffix = rx_suffix_word.search(line)
            has_ambiguous = rx_ambiguous.search(line)
            if not has_suffix and not has_ambiguous:
                continue

            # "operated by X Ltd" patterns (unambiguous suffixes)
            for m in rx_operated.finditer(line):
                name = m.group(1).strip(" .\t\n\r")
                if 3 < len(name) < 120:
                    candidates[name] = True
            # Entity name + suffix: 1-6 capitalised words followed by a legal suffix
            # e.g. "Herculite Products Inc", "Amazon Web Services LLC", "Herculite, Inc."
            for m in rx_entity_suffix.finditer(line):
                name = m.group(1).strip(" .\t\n\r")
                if 3 < len(name) < 120:
                    candidates[name] = True
            # Ambiguous suffixes: "Siemens AG", "Equinor AS" (case-sensitive, require uppercase preceding word)
            for m in rx_ambiguous.finditer(line):
                name = (m.group(1) + ' ' + m.group(2)).strip()
                if 3 < len(name) < 120:
                    candidates[name] = True

        # 3. Filter out entity type descriptions (not actual entity names)
        entity_type_pattern = re.compile(
            r"^(Domestic|Foreign|Registered|Gen\.?|Non-?Profit|For-?Profit|Mutual)\b", re.I)
        entity_type_fragment_pattern = re.compile(
            r"^(Liability|Profit|Stock|Business|General)\s+(Company|Corporation|Partnership|LLC)", re.I)
        entity_type_exact = [
            'corporation', 'business corporation', 'profit corporation', 'stock corporation',
            'limited partnership', 'limited-liability company', 'limited liability company',
            'liability company', 'profit business corporation',
            'trade name', 'fictitious name', 'dba', 'dpc', 'business', 'partnership',
            'corporation for profit', 'general partnership', 'sole proprietorship',
        ]
        for name in list(candidates.keys()):
            norm = name.strip(' .').lower()
            if entity_type_pattern.search(name) or entity_type_fragment_pattern.search(name) \
                    or norm in entity_type_exact:
                del candidates[name]

        # 4. Deduplicate: keep longest forms, remove substrings
        result = list(candidates.keys())
        result.sort(key=len, reverse=True)
        final = []
        for name in result:
            is_substring = False
            for other in final:
                if name != other and name.lower() in other.lower():
                    is_substring = True
                    break
            if not is_substring:
                final.append(name)

        return final[:limit]

    # ── Phase 2: LLM Entity Extraction ──────────────────────────────────────

    def extract_entities_with_llm(self, website_data, google_intel_registries=None):
        # PHP extractEntitiesWithLlm(array $websiteData, array $googleIntelRegistries = []): array
        if google_intel_registries is None:
            google_intel_registries = {}

        parts = []
        seen_lines = {}
        for page_name, text in website_data['pages'].items():
            truncated = text[:3000]
            # For contact/about pages, also include the tail (addresses often near bottom)
            if len(text) > 3500 and re.search(
                    r'contact|about|impressum|imprint|leadership|management|team|disclosures?|regulatory|news',
                    page_name, re.I):
                tail = text[-1000:]
                truncated += "\n[...]\n" + tail
            # Deduplicate lines that appeared on previous pages (nav, footer, cookie banners)
            lines = truncated.split("\n")
            filtered = []
            for line in lines:
                trimmed = line.strip()
                if len(trimmed) >= 30 and trimmed in seen_lines:
                    continue
                filtered.append(line)
                if len(trimmed) >= 30:
                    seen_lines[trimmed] = True
            parts.append("=== " + page_name.upper() + " ===\n" + "\n".join(filtered))
        parts.append("=== WHOIS ===\n" + self.scrub_blocked_names(website_data['whois']))

        # Include Google Intelligence data (LinkedIn, Yahoo Finance, Google results)
        for key, data in google_intel_registries.items():
            if data:
                label = key.split(':')[0].replace('_', ' ').upper()
                parts.append("=== " + label + " ===\n" + data[:3000])
        user_msg = "\n\n".join(parts)

        self.log('llm', f"LLM extraction — calling {self.config['model']}", {
            'expandable': True,
            'sections': [
                {'label': 'System Prompt', 'content': self.extraction_prompt},
                {'label': f"User Input ({len(user_msg):,} chars)", 'content': user_msg},
            ],
        })

        response_text = self.call_llm(self.extraction_prompt, user_msg, 2048)

        self.log('llm', "LLM extraction output:", {
            'expandable': True,
            'sections': [{'label': 'Response JSON', 'content': response_text}],
        })

        # Parse JSON from response
        result = self.parse_json_response(response_text, {
            'entity_names': [],
            'short_names': [],
            'jurisdiction': 'unknown',
        })

        # If the LLM knows a parent entity, add it to entity_names
        if result.get('known_parent'):
            result.setdefault('entity_names', []).append(result['known_parent'])
            self.log('llm', f"LLM knows parent entity: {result['known_parent']}")
        if result.get('known_jurisdiction'):
            self.log('llm', f"LLM knows parent jurisdiction: {result['known_jurisdiction']}")
        if result.get('llm_notes'):
            self.log('llm', f"LLM notes: {result['llm_notes']}")

        return result

    # ── Phase 3 helper: Deduplicate entity names ────────────────────────────

    def deduplicate_names(self, names):
        """
        PHP deduplicateNames(array $names): array
        Deduplicate entity names by normalizing punctuation and suffix variants.
        "Inc." vs "Inc" vs "Incorporated" are treated as the same suffix.
        Keeps the longest original form.
        """
        # Suffix equivalences: map short forms to canonical long form.
        # Order matters (PHP array order) — first matching suffix wins.
        suffix_map = {
            'inc': 'incorporated', 'inc.': 'incorporated',
            'corp': 'corporation', 'corp.': 'corporation',
            'ltd': 'limited', 'ltd.': 'limited',
            'co': 'company', 'co.': 'company',
            'plc': 'public limited company', 'p.l.c.': 'public limited company', 'p.l.c': 'public limited company',
            'llc': 'limited liability company', 'l.l.c.': 'limited liability company', 'l.l.c': 'limited liability company',
            'llp': 'limited liability partnership', 'l.l.p.': 'limited liability partnership', 'l.l.p': 'limited liability partnership',
            'lp': 'limited partnership', 'l.p.': 'limited partnership', 'l.p': 'limited partnership',
            'ag': 'aktiengesellschaft',
            'gmbh': 'gesellschaft mit beschraenkter haftung', 'mbh': 'gesellschaft mit beschraenkter haftung',
            'sa': 'societe anonyme', 's.a.': 'societe anonyme', 's.a': 'societe anonyme',
            'sas': 'societe par actions simplifiee', 's.a.s.': 'societe par actions simplifiee', 's.a.s': 'societe par actions simplifiee',
            'sarl': 'societe a responsabilite limitee', 's.a.r.l.': 'societe a responsabilite limitee',
            'bv': 'besloten vennootschap', 'b.v.': 'besloten vennootschap', 'b.v': 'besloten vennootschap',
            'nv': 'naamloze vennootschap', 'n.v.': 'naamloze vennootschap', 'n.v': 'naamloze vennootschap',
            'se': 'societas europaea',
            'kg': 'kommanditgesellschaft', 'kgaa': 'kommanditgesellschaft auf aktien',
            'ab': 'aktiebolag',
            'as': 'aksjeselskap', 'a.s.': 'aksjeselskap', 'a.s': 'aksjeselskap', 'a/s': 'aksjeselskap',
            'aps': 'anpartsselskab',
            'oy': 'osakeyhtio', 'oyj': 'osakeyhtio julkinen',
            'srl': 'societa a responsabilita limitata', 's.r.l.': 'societa a responsabilita limitata',
            'spa': 'societa per azioni', 's.p.a.': 'societa per azioni',
            'sl': 'sociedad limitada', 's.l.': 'sociedad limitada',
            'pty ltd': 'proprietary limited', 'pty ltd.': 'proprietary limited',
            'pty. ltd': 'proprietary limited', 'pty. ltd.': 'proprietary limited',
            'co ltd': 'company limited', 'co. ltd': 'company limited',
            'co ltd.': 'company limited', 'co. ltd.': 'company limited',
        }

        normalized = {}  # normalizedKey => original name
        for n in names:
            n = n.strip()
            if len(n) <= 1:
                continue

            # Build normalized key: lowercase, strip punctuation, normalize suffix
            key = n.lower()
            key = key.replace(',', '').replace('.', '')
            key = re.sub(r'\s+', ' ', key.strip())

            # Replace suffix at end of name with canonical form
            for short, long in suffix_map.items():
                short_clean = short.replace(',', '').replace('.', '')
                if key.endswith(' ' + short_clean):
                    key = key[:len(key) - len(short_clean)] + long
                    break
                if key.endswith(' ' + long.replace(',', '').replace('.', '')):
                    break  # already canonical

            if not key:
                continue
            # Keep the longer original form
            if key not in normalized or len(n) > len(normalized[key]):
                normalized[key] = n
        return list(normalized.values())
