"""
Search-coverage test harness — checks that expected entity names appear in the registry
EVIDENCE, WITHOUT running the (expensive, non-deterministic) analysis LLM or judging the final
recommendation. Every retrieval bug we've hit (herculite's starved search, questglobal's 0
registries, the Singapore gap) is a Phase-4 failure this catches deterministically.

A case runs the pipeline up to Phase 4 (search_registries) and greps the concatenated evidence:
  - expect[]            : each string MUST appear (case-insensitive)
  - forbid[]            : each string must NOT appear
  - expect_in_source{}  : {source: [strings]} — string must appear in THAT source's block (e.g. the
                          Singapore UEN must come from acra:, not incidentally from northdata)
  - max_calls{}         : {service: n} — api_calls must stay under n (catches the sweep regression)

Two modes: url=... (fetch + extract + search — one cheap extraction LLM call) or
names=[...]+jurisdiction (search only — zero LLM, fully deterministic).

FAIL vs INCONCLUSIVE: a registry that throttled/errored (empty for a non-real reason) is NOT a
regression — those blocks carry our "throttled/error" markers, so a missing expect with a throttle
present is reported INCONCLUSIVE (warn, don't fail CI) rather than FAIL.
"""
import json
import os
import re
from contextlib import closing
from urllib.parse import urlparse

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover
    psycopg2 = None

DSN = os.environ.get("DATABASE_URL")
_THROTTLE_MARKERS = ("throttled/error", "no response", "not a confirmed empty",
                     "not a real 0", "budget", "http 0", "error/throttled")


# ── evidence builder (runs up to Phase 4, never Phase 6/analysis) ──────────
def _domain(url: str) -> str:
    return re.sub(r'^www\.', '', (urlparse(url).hostname or ''))


def _model_rates(model: str):
    """($/M input, $/M output) — mirrors the agent's pricing table so cost matches a real run."""
    m = model or ''
    if 'haiku' in m:
        return (0.80, 4.00)
    if 'opus' in m:
        return (15.00, 75.00)
    if 'sonnet' in m:
        return (3.00, 15.00)
    if m == 'gpt-4o-mini':
        return (0.15, 0.60)
    if m == 'gpt-4o':
        return (2.50, 10.00)
    return (3.00, 15.00)


def build_evidence(config, url=None, names=None, jurisdiction=None):
    """Run the retrieval phases and return (evidence_blob_by_source, api_calls, info, cost).
    url mode → fetch + extract + search (1 extraction LLM call, so a small cost). names mode →
    search only (no LLM, cost 0). `cost` = {input_tokens, output_tokens, cost_usd}."""
    from agent import EntityLookup
    agent = EntityLookup(config, progress_callback=None)
    if url:
        domain = _domain(url)
        website_data = agent.fetch_website_data(url, domain)
        info = agent.extract_entities_with_llm(website_data, {})
        info['entity_names'] = agent.deduplicate_names(info.get('entity_names') or [])
    else:
        domain = ''
        info = {'entity_names': list(names or []), 'short_names': [],
                'jurisdiction': (jurisdiction or 'unknown')}
    registries = agent.search_registries(info, domain)
    # keep values as strings, keyed by source, for source-scoped grep
    blob = {k: (v if isinstance(v, str) else json.dumps(v, default=str))
            for k, v in registries.items()}
    it, ot = agent.total_input_tokens, agent.total_output_tokens
    ri, ro = _model_rates(config.get('model') or '')
    cost = {'input_tokens': it, 'output_tokens': ot,
            'cost_usd': round(it * ri / 1_000_000 + ot * ro / 1_000_000, 4)}
    return blob, agent.tools.get_api_calls(), info, cost


# ── one case → structured result ──────────────────────────────────────────
def run_case(config, case: dict) -> dict:
    """Build the evidence for a case and grep it. Returns a structured result with per-check
    outcomes, the api_calls, and a short evidence excerpt for inspection."""
    try:
        blob, api_calls, info, cost = build_evidence(
            config, url=case.get('url'),
            names=case.get('names'), jurisdiction=case.get('jurisdiction'))
    except Exception as e:  # noqa: BLE001
        return {'case': case.get('name'), 'status': 'error', 'error': str(e), 'checks': [], 'cost_usd': 0}

    full = "\n".join(f"[{k}]\n{v}" for k, v in blob.items())
    full_l = full.lower()
    throttled = any(m in full_l for m in _THROTTLE_MARKERS)

    checks = []
    hard_fail = False
    inconclusive = False

    for s in (case.get('expect') or []):
        found = s.lower() in full_l
        if found:
            checks.append({'kind': 'expect', 'text': s, 'status': 'pass'})
        elif throttled:
            checks.append({'kind': 'expect', 'text': s, 'status': 'inconclusive',
                           'note': 'a registry throttled/errored — not a confirmed miss'})
            inconclusive = True
        else:
            checks.append({'kind': 'expect', 'text': s, 'status': 'fail'})
            hard_fail = True

    for s in (case.get('forbid') or []):
        if s.lower() in full_l:
            checks.append({'kind': 'forbid', 'text': s, 'status': 'fail'})
            hard_fail = True
        else:
            checks.append({'kind': 'forbid', 'text': s, 'status': 'pass'})

    for src, strings in (case.get('expect_in_source') or {}).items():
        src_blob = "\n".join(v for k, v in blob.items()
                             if k.split(':')[0].lower() == src.lower()).lower()
        for s in strings:
            if s.lower() in src_blob:
                checks.append({'kind': 'in_source', 'text': f'{s} @ {src}', 'status': 'pass'})
            elif throttled:
                checks.append({'kind': 'in_source', 'text': f'{s} @ {src}', 'status': 'inconclusive'})
                inconclusive = True
            else:
                checks.append({'kind': 'in_source', 'text': f'{s} @ {src}', 'status': 'fail'})
                hard_fail = True

    for svc, limit in (case.get('max_calls') or {}).items():
        used = api_calls.get(svc, 0)
        ok = used <= limit
        checks.append({'kind': 'budget', 'text': f'{svc} ≤ {limit} (used {used})',
                       'status': 'pass' if ok else 'fail'})
        if not ok:
            hard_fail = True

    status = 'fail' if hard_fail else ('inconclusive' if inconclusive else 'pass')
    return {
        'case': case.get('name'),
        'status': status,
        'checks': checks,
        'api_calls': {k: v for k, v in api_calls.items() if v},
        'cost_usd': cost['cost_usd'],
        'tokens': {'input': cost['input_tokens'], 'output': cost['output_tokens']},
        'mode': 'url' if case.get('url') else 'names',
        'extracted_names': info.get('entity_names'),
        'jurisdiction': info.get('jurisdiction'),
        'evidence_excerpt': full[:4000],
    }


# ── persistent case storage (Postgres) ─────────────────────────────────────
def enabled() -> bool:
    return bool(DSN and psycopg2)


def _conn():
    return psycopg2.connect(DSN)


def ensure_schema() -> None:
    if not enabled():
        return
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE SCHEMA IF NOT EXISTS entity;
                CREATE TABLE IF NOT EXISTS entity.coverage_cases (
                    id           bigserial PRIMARY KEY,
                    name         text NOT NULL,
                    url          text,
                    names        jsonb,
                    jurisdiction text,
                    expect       jsonb NOT NULL DEFAULT '[]',
                    forbid       jsonb NOT NULL DEFAULT '[]',
                    expect_in_source jsonb NOT NULL DEFAULT '{}',
                    max_calls    jsonb NOT NULL DEFAULT '{}',
                    created_at   timestamptz NOT NULL DEFAULT now()
                );
            """)
        c.commit()
    _seed_if_empty()


def _row(r):
    """dict-ify a case row, making it JSON-safe (created_at → isoformat)."""
    d = dict(r)
    if d.get("created_at") is not None:
        d["created_at"] = d["created_at"].isoformat()
    return d


def list_cases() -> list:
    if not enabled():
        return []
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM entity.coverage_cases ORDER BY id")
            return [_row(r) for r in cur.fetchall()]


def get_case(cid: int) -> dict | None:
    if not enabled():
        return None
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM entity.coverage_cases WHERE id=%s", (cid,))
            r = cur.fetchone()
            return _row(r) if r else None


def add_case(case: dict) -> int:
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.coverage_cases "
                "(name,url,names,jurisdiction,expect,forbid,expect_in_source,max_calls) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (case.get('name') or 'unnamed', case.get('url'),
                 json.dumps(case.get('names') or None), case.get('jurisdiction'),
                 json.dumps(case.get('expect') or []), json.dumps(case.get('forbid') or []),
                 json.dumps(case.get('expect_in_source') or {}),
                 json.dumps(case.get('max_calls') or {})))
            cid = cur.fetchone()[0]
        c.commit()
    return cid


def update_case(cid: int, case: dict) -> None:
    """Overwrite a case's fields in place (for the edit form)."""
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE entity.coverage_cases SET name=%s, url=%s, names=%s, jurisdiction=%s, "
                "expect=%s, forbid=%s, expect_in_source=%s, max_calls=%s WHERE id=%s",
                (case.get('name') or 'unnamed', case.get('url'),
                 json.dumps(case.get('names') or None), case.get('jurisdiction'),
                 json.dumps(case.get('expect') or []), json.dumps(case.get('forbid') or []),
                 json.dumps(case.get('expect_in_source') or {}),
                 json.dumps(case.get('max_calls') or {}), cid))
        c.commit()


def delete_case(cid: int) -> None:
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM entity.coverage_cases WHERE id=%s", (cid,))
        c.commit()


# Starter fixtures — seeded once, from cases we've already validated this session.
_SEED = [
    {"name": "herculite → Aberdeen (DBA owner + call budget)", "url": "https://herculite.com",
     "expect": ["ABERDEEN ROAD COMPANY", "2887899"], "max_calls": {"bizapedia": 20}},
    {"name": "questglobal → Quest Global Services Pte. Ltd. (Singapore UEN from ACRA)",
     "url": "https://questglobal.com",
     "expect": ["200904830K", "QUEST GLOBAL SERVICES PTE. LTD."],
     "expect_in_source": {"acra": ["200904830K"]}},
    {"name": "Alianza LLC → Delaware parent (branch triangulation)", "names": ["Alianza, LLC"],
     "jurisdiction": "us", "expect": ["ALIANZA, L.L.C.", "2987760"], "max_calls": {"bizapedia": 12}},
    {"name": "NexPhase Capital → Delaware LPs (Bizapedia)", "names": ["NexPhase Capital"],
     "jurisdiction": "us", "expect": ["NEXPHASE CAPITAL", "DE"]},
    # UK private company — exercises Companies House + the deep PSC ownership-chain traversal all
    # the way to the ULTIMATE parent (a listed company would lean on Yahoo Finance and not really
    # test Companies House). Entity #06294877 → … → Vulcan1 TopCo #16483240 (7 levels).
    {"name": "ABCA Systems → Companies House + ownership chain to ultimate parent (UK private)",
     "names": ["ABCA Systems Ltd"], "jurisdiction": "uk",
     "expect": ["ABCA SYSTEMS LIMITED", "06294877", "VULCAN1 TOPCO", "16483240"],
     "expect_in_source": {"companies_house": ["06294877"], "ownership_chain": ["16483240"]}},
]


def _seed_if_empty():
    try:
        if not list_cases():
            for c in _SEED:
                add_case(c)
    except Exception as e:  # noqa: BLE001
        print(f"[coverage] seed skipped: {e}")
