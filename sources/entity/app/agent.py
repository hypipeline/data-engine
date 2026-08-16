"""
Entity Lookup v3b (Python) — orchestration.

Faithful port of php/lookup.php (class EntityLookup): the 8-phase run() pipeline, the
LLM layer (Claude + OpenAI with token/cost accounting), JSON parsing, and every phase
method. Uses tools.LookupTools (composed from toolbase + the tools_*.py mixins).

Logging matches the PHP exactly: log(phase, message, detail) where detail may carry
{'expandable': True, 'sections': [{'label','content'}]} — streamed to the UI.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from tools import LookupTools
from phases_website import WebsiteFetchMixin
from phases_extract import ExtractionMixin
from phases_registry import RegistrySearchMixin
from phases_analysis import EvidenceAnalysisMixin
from phases_validate import ValidationMixin

_PROMPTS = Path(__file__).parent / "prompts"


class EntityLookup(WebsiteFetchMixin, ExtractionMixin, RegistrySearchMixin,
                   EvidenceAnalysisMixin, ValidationMixin):
    def __init__(self, config: dict, progress_callback=None):
        self.config = config
        self.timings: dict = {}
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.progress_log: list = []
        self.start_time = 0.0
        self.progress_callback = progress_callback

        # tools get a progress callback that also records into progress_log (PHP __construct)
        def tools_progress(entry):
            entry.setdefault('time', round(time.time() - self.start_time, 2))
            entry.setdefault('detail', None)
            self.progress_log.append(entry)
            if progress_callback:
                progress_callback(entry)
        self.tools = LookupTools(config, tools_progress if progress_callback is not None else None)

        self.extraction_prompt = (_PROMPTS / "extraction.txt").read_text()
        self.analysis_prompt = (_PROMPTS / "analysis.txt").read_text()
        self.json_schema = (_PROMPTS / "schema.txt").read_text()

    # ── logging (PHP log()) ─────────────────────────────────────────────────
    def log(self, phase: str, message: str, detail: dict | None = None) -> None:
        entry = {
            'time': round(time.time() - self.start_time, 2),
            'phase': phase,
            'message': message,
            'detail': detail,
        }
        self.progress_log.append(entry)
        if self.progress_callback:
            self.progress_callback(entry)

    # ── the 8-phase pipeline (PHP run()) ────────────────────────────────────
    def run(self, url: str) -> dict:
        self.start_time = time.time()
        t0 = self.start_time
        host = (urlparse(url).hostname or '')
        domain = re.sub(r'^www\.', '', host)

        self.log('start', f"Beginning lookup for {domain}")

        # Phase 1: Google Intelligence
        self.log('phase', "Phase 1: Google Intelligence", {'phase_num': 1})
        t_google = time.time()
        google_intel = self.tools.google_intelligence(domain)
        registries: dict = {}

        if google_intel.get('google_results'):
            registries['google_search'] = google_intel['google_results']
            self.log('google', f"Google search: {google_intel['google_results'].count(chr(10))} lines", {
                'expandable': True,
                'sections': [{'label': 'Google Results', 'content': google_intel['google_results']}],
            })

        if google_intel.get('linkedin_url'):
            linkedin_data = self.tools.fetch_linkedin_company(google_intel['linkedin_url'])
            if linkedin_data:
                registries['linkedin'] = linkedin_data
                self.log('google', f"LinkedIn: {linkedin_data.count(chr(10))} lines", {
                    'expandable': True,
                    'sections': [{'label': 'LinkedIn Data', 'content': linkedin_data}],
                })

        if google_intel.get('yahoo_ticker'):
            yahoo_data = self.tools.yahoo_finance_data(google_intel['yahoo_ticker'])
            if yahoo_data:
                ticker = google_intel['yahoo_ticker']
                registries[f"yahoo_finance:{ticker}"] = yahoo_data
                self.log('google', f"Yahoo Finance: {ticker} — {yahoo_data.count(chr(10))} lines", {
                    'expandable': True,
                    'sections': [{'label': 'Yahoo Finance Data', 'content': yahoo_data}],
                })

        self.timings['google_intel'] = time.time() - t_google
        self.log('google', f"Google Intelligence complete in {round(self.timings['google_intel'], 1)}s")

        # Phase 2: Fetch website
        self.log('phase', "Phase 2: Fetch Website Data", {'phase_num': 2})
        website_data = self.fetch_website_data(url, domain)
        self.timings['fetch'] = time.time() - t0
        self.log('fetch', f"Website fetched in {round(self.timings['fetch'], 1)}s — pages: "
                 + ", ".join(website_data['pages'].keys()))

        # Phase 3: Extract entities with LLM (include Google Intelligence data)
        self.log('phase', "Phase 3: Extract Entity Names", {'phase_num': 3})
        t1 = time.time()
        entity_info = self.extract_entities_with_llm(website_data, registries)
        self.timings['extraction'] = time.time() - t1
        self.log('extract', f"Extraction complete in {round(self.timings['extraction'], 1)}s — names: "
                 + json.dumps(entity_info.get('entity_names'))
                 + ", short_names: " + json.dumps(entity_info.get('short_names', []))
                 + ", jurisdiction: " + (entity_info.get('jurisdiction') or 'unknown'))

        deduped = self.deduplicate_names(entity_info.get('entity_names') or [])
        self.log('extract', f"Deduplicated names: {len(entity_info.get('entity_names') or [])} → {len(deduped)}"
                 + "\n  Before: " + json.dumps(entity_info.get('entity_names') or [])
                 + "\n  After:  " + json.dumps(deduped))
        entity_info['entity_names'] = deduped

        # Phase 4: Search registries
        self.log('phase', "Phase 4: Search Registries", {'phase_num': 4})
        t2 = time.time()
        new_registries = self.search_registries(entity_info, domain)
        registries.update(new_registries)
        self.timings['registries'] = time.time() - t2
        self.log('registry', f"All registry searches complete in {round(self.timings['registries'], 1)}s — "
                 f"{len(new_registries)} results")

        # Phase 5: Evidence chain
        self.log('phase', "Phase 5: Evidence Chain — Connecting Website to Entities", {'phase_num': 5})
        cross_ref = self.cross_reference_sec_data(website_data, registries, entity_info)
        if cross_ref:
            registries['sec_cross_reference'] = cross_ref

        # Phase 6: Final LLM analysis
        self.log('phase', "Phase 6: Final Analysis", {'phase_num': 6})
        t3 = time.time()
        input_before = self.total_input_tokens
        output_before = self.total_output_tokens
        report = self.analyze_evidence(url, domain, website_data, entity_info, registries)
        self.timings['analysis'] = time.time() - t3
        analysis_cost = ((self.total_input_tokens - input_before) * 3.0 / 1_000_000) \
            + ((self.total_output_tokens - output_before) * 15.0 / 1_000_000)
        self.log('llm', f"Analysis complete in {round(self.timings['analysis'], 1)}s — "
                 f"{self.total_input_tokens - input_before:,} input / "
                 f"{self.total_output_tokens - output_before:,} output tokens — ${analysis_cost:.4f}")

        # Phase 7: Registry validation
        entity = report.get('recommended_entity')
        has_registry_id = bool(entity and entity.get('registry_id'))
        if has_registry_id:
            self.log('phase', "Phase 7: Registry Validation", {'phase_num': 7})
            report = self.validate_entity_in_registry(report)

        # Phase 8: Re-analysis — OFF by default. The second LLM pass proved net-negative in practice
        # (it timed out on the heaviest inputs and over-abstained, discarding good Phase-6 answers
        # more often than it improved them). We now KEEP the Phase-6 recommendation and just flag the
        # validation outcome. Re-enable the second pass by setting config 'reanalysis_enabled'.
        rv_status = (report.get('registry_validation') or {}).get('status')
        needs_reanalysis = (rv_status and rv_status != 'verified') or (entity and not has_registry_id)
        if needs_reanalysis and self.config.get('reanalysis_enabled'):
            reason = f"validation failed: {rv_status}" if rv_status else "no registry_id on recommended entity"
            self.log('phase', f"Phase 8: Re-analysis ({reason})", {'phase_num': 8})
            t8 = time.time()
            report = self.reanalyze_after_validation_failure(
                report, url, domain, website_data, entity_info, registries)
            self.timings['reanalysis'] = time.time() - t8

        # Flag a non-verified registry validation on the KEPT recommendation (no second LLM pass).
        rv_status = (report.get('registry_validation') or {}).get('status')
        if rv_status and rv_status != 'verified':
            report['confidence'] = 'low'
            report['validation_warning'] = (f"Registry validation not verified (status: {rv_status}) — "
                                            "confidence downgraded; recommendation kept without a second LLM pass.")
            self.log('warning', f"Confidence downgraded to 'low': registry validation status is '{rv_status}'")

        # Validate contractable affiliates
        affiliates = report.get('contractable_affiliates') or []
        if affiliates:
            self.log('validate', f"Validating {len(affiliates)} contractable affiliate(s)...")
            for aff in affiliates:
                aff_name = aff.get('legal_entity_name') or ''
                aff_reg = aff.get('registry_id') or ''
                if not aff_name or not aff_reg:
                    aff['validation_status'] = 'no_registry_id'
                    aff['registry_validated'] = False
                    continue
                aff_country = (aff.get('jurisdiction_country') or '').upper()
                aff_state = (aff.get('jurisdiction_state') or '').upper()
                v = self.validate_single_entity(aff_name, aff_reg, aff_country, aff_state or None)
                aff['validation_status'] = v['status']
                aff['registry_validated'] = (v['status'] == 'verified')
                if v.get('validation_url'):
                    aff['validation_url'] = v['validation_url']
                if v.get('registry_name'):
                    aff['registry_name'] = v['registry_name']
                if v.get('source'):
                    aff['validation_source'] = v['source']
                self.log('validate', f"Affiliate \"{aff_name}\": {v['status']}"
                         + (f" ({v['source']})" if v.get('source') else ''))
            report['contractable_affiliates'] = affiliates

        entity = report.get('recommended_entity')
        confidence = report.get('confidence') or 'unknown'
        self.log('done', (f"Result: {entity['legal_entity_name']} ({confidence})" if entity
                          else f"Result: No entity found ({confidence})"))

        total_time = time.time() - t0
        model = self.config.get('model') or ''
        if 'haiku' in model:
            rates = (0.80, 4.00)
        elif 'opus' in model:
            rates = (15.00, 75.00)
        elif 'sonnet' in model:
            rates = (3.00, 15.00)
        elif model == 'gpt-4o-mini':
            rates = (0.15, 0.60)
        elif model == 'gpt-4o':
            rates = (2.50, 10.00)
        elif model == 'o3':
            rates = (2.00, 8.00)
        elif model == 'o4-mini':
            rates = (1.10, 4.40)
        else:
            rates = (3.00, 15.00)
        cost = (self.total_input_tokens * rates[0] / 1_000_000) + (self.total_output_tokens * rates[1] / 1_000_000)

        # Tool-use summary — surfaced in the report meta AND as an end-of-run log line so it is
        # visible in the main entity-lookup run, not only in tests.
        usage = self.tools.usage_summary()
        def _fmt(d):
            return ', '.join(f"{k} {v}" for k, v in d.items()) or '—'
        cache_note = ''
        if usage['cached']:
            cache_note = '  |  cached: ' + _fmt(usage['cached'])
        self.log('summary',
                 f"Tool usage — {usage['total']} calls total. "
                 f"Sources: {_fmt(usage['sources'])}. "
                 f"Transport: {_fmt(usage['transport'])}." + cache_note,
                 {'expandable': True, 'sections': [
                     {'label': 'Per-operation breakdown', 'content': _fmt(usage['detail'])}]})

        return {
            'report': report,
            'meta': {
                'total_time_s': round(total_time, 1),
                'phase_times': {k: round(v, 1) for k, v in self.timings.items()},
                'model': self.config.get('model'),
                'input_tokens': self.total_input_tokens,
                'output_tokens': self.total_output_tokens,
                'cost_usd': round(cost, 4),
                'api_calls': self.tools.get_api_calls(),
                'usage': usage,
            },
            'progress_log': self.progress_log,
        }

    # ── LLM layer (PHP callLLM/callClaude/callOpenAI/parseJsonResponse) ──────
    def _is_openai_model(self) -> bool:
        m = self.config.get('model') or ''
        return m.startswith('gpt-') or m.startswith('o1') or m.startswith('o3') or m.startswith('o4')

    def call_llm(self, system_prompt: str, user_message: str, max_tokens: int, op: str | None = None) -> str:
        if self._is_openai_model():
            return self.call_openai(system_prompt, user_message, max_tokens, op=op)
        return self.call_claude(system_prompt, user_message, max_tokens, op=op)

    def call_claude(self, system_prompt: str, user_message: str, max_tokens: int, op: str | None = None) -> str:
        self.tools.count('claude', op=op)
        input_chars = len(system_prompt) + len(user_message)
        self.log('llm', f"Calling Claude ({self.config['model']}) — input: {input_chars:,} chars, max_tokens: {max_tokens}")
        try:
            r = requests.post('https://api.anthropic.com/v1/messages',
                              headers={'Content-Type': 'application/json',
                                       'x-api-key': self.config['anthropic_api_key'],
                                       'anthropic-version': '2023-06-01'},
                              json={'model': self.config['model'], 'max_tokens': max_tokens,
                                    'system': system_prompt,
                                    'messages': [{'role': 'user', 'content': user_message}]},
                              timeout=120)
        except requests.RequestException as e:
            self.log('llm', f"ERROR: Claude API request failed — {e}")
            return json.dumps({'error': 'Claude API request failed'})
        if r.status_code != 200 or not r.text:
            try:
                err = (r.json().get('error') or {}).get('message')
            except Exception:
                err = r.text or 'No response body'
            self.log('llm', f"ERROR: Claude API returned HTTP {r.status_code} — {err}")
            return json.dumps({'error': f"Claude API returned HTTP {r.status_code}"})
        data = r.json()
        usage = data.get('usage') or {}
        it = usage.get('input_tokens', 0)
        ot = usage.get('output_tokens', 0)
        self.total_input_tokens += it
        self.total_output_tokens += ot
        text = ''.join(b.get('text', '') for b in (data.get('content') or []) if b.get('type') == 'text')
        stop = data.get('stop_reason', 'unknown')
        call_cost = (it * 3.0 / 1_000_000) + (ot * 15.0 / 1_000_000)
        self.log('llm', f"Response: {it:,} input / {ot:,} output tokens — ${call_cost:.4f}")
        if stop == 'max_tokens':
            self.log('llm', f"WARNING: Response truncated (hit max_tokens={max_tokens}). Output may be incomplete.")
        return text

    def call_openai(self, system_prompt: str, user_message: str, max_tokens: int, op: str | None = None) -> str:
        self.tools.count('openai', op=op)
        input_chars = len(system_prompt) + len(user_message)
        model = self.config['model']
        self.log('llm', f"Calling OpenAI ({model}) — input: {input_chars:,} chars, max_tokens: {max_tokens}")
        try:
            r = requests.post('https://api.openai.com/v1/chat/completions',
                              headers={'Content-Type': 'application/json',
                                       'Authorization': f"Bearer {self.config['openai_api_key']}"},
                              json={'model': model, 'max_completion_tokens': max_tokens,
                                    'messages': [{'role': 'system', 'content': system_prompt},
                                                 {'role': 'user', 'content': user_message}]},
                              timeout=120)
        except requests.RequestException as e:
            self.log('llm', f"ERROR: OpenAI API request failed — {e}")
            return json.dumps({'error': 'OpenAI API request failed'})
        if r.status_code != 200 or not r.text:
            try:
                err = (r.json().get('error') or {}).get('message')
            except Exception:
                err = r.text or 'No response body'
            self.log('llm', f"ERROR: OpenAI API returned HTTP {r.status_code} — {err}")
            return json.dumps({'error': f"OpenAI API returned HTTP {r.status_code}"})
        data = r.json()
        usage = data.get('usage') or {}
        it = usage.get('prompt_tokens', 0)
        ot = usage.get('completion_tokens', 0)
        self.total_input_tokens += it
        self.total_output_tokens += ot
        text = (((data.get('choices') or [{}])[0].get('message') or {}).get('content')) or ''
        finish = (data.get('choices') or [{}])[0].get('finish_reason', 'unknown')
        call_cost = (it * 2.5 / 1_000_000) + (ot * 10.0 / 1_000_000)
        self.log('llm', f"Response: {it:,} input / {ot:,} output tokens — ${call_cost:.4f}")
        if finish == 'length':
            self.log('llm', f"WARNING: Response truncated (hit max_tokens={max_tokens}). Output may be incomplete.")
        return text

    def parse_json_response(self, text: str, fallback: dict) -> dict:
        clean = (text or '').strip()
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', clean, re.S)
        if m:
            clean = m.group(1)
        try:
            result = json.loads(clean)
            if isinstance(result, (dict, list)):
                return result
        except Exception:
            pass
        start = clean.find('{')
        if start != -1:
            depth = 0
            for i in range(start, len(clean)):
                if clean[i] == '{':
                    depth += 1
                elif clean[i] == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            result = json.loads(clean[start:i + 1])
                            if isinstance(result, (dict, list)):
                                return result
                        except Exception:
                            pass
                        break
        # Last resort: the response was truncated mid-JSON (e.g. hit max_tokens on a very large
        # group). Salvage the largest complete prefix by dropping the incomplete trailing element
        # and closing the brackets that were still open at the last completed value.
        salvaged = self._salvage_truncated_json(clean)
        if isinstance(salvaged, dict):
            salvaged.setdefault('note', 'Report recovered from a truncated LLM response — some '
                                        'trailing entities may be missing.')
            return salvaged
        return fallback

    @staticmethod
    def _salvage_truncated_json(s: str):
        """Best-effort repair of a truncated JSON object: walk to the last completed
        object/array, then append the closers for whatever was still open there."""
        start = s.find('{')
        if start == -1:
            return None
        in_str = esc = False
        stack = []            # expected closing chars, in open order
        cut = None            # index just after the last completed '}'/']'
        cut_stack = None      # bracket stack snapshot at that point
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in '{[':
                stack.append('}' if ch == '{' else ']')
            elif ch in '}]':
                if stack:
                    stack.pop()
                cut, cut_stack = i, list(stack)
        if cut is None or cut_stack is None:
            return None
        candidate = s[start:cut + 1] + ''.join(reversed(cut_stack))
        try:
            result = json.loads(candidate)
            return result if isinstance(result, dict) else None
        except Exception:
            return None

    # ── phase methods (ported below) ────────────────────────────────────────
    # fetch_website_data, extract_entities_with_llm, deduplicate_names,
    # search_registries, cross_reference_sec_data, analyze_evidence,
    # validate_entity_in_registry, validate_single_entity,
    # reanalyze_after_validation_failure, and helpers — added in agent_phases.py
    #   (imported as methods) to keep this file navigable.
