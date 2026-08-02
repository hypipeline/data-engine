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


def _google_intel_registries(agent, domain):
    """Phase 1 (Google Intelligence) → the google/linkedin/yahoo evidence blocks, mirroring the
    agent's run(). Only used when a case opts into include_google (adds Bright Data/Browserbase
    calls + latency, so it's off by default)."""
    reg = {}
    gi = agent.tools.google_intelligence(domain)
    if gi.get('google_results'):
        reg['google_search'] = gi['google_results']
    if gi.get('linkedin_url'):
        ld = agent.tools.fetch_linkedin_company(gi['linkedin_url'])
        if ld:
            reg['linkedin'] = ld
    if gi.get('yahoo_ticker'):
        yd = agent.tools.yahoo_finance_data(gi['yahoo_ticker'])
        if yd:
            reg[f"yahoo_finance:{gi['yahoo_ticker']}"] = yd
    return reg


def build_evidence(config, url=None, names=None, jurisdiction=None, include_google=False, refresh=False):
    """Run the retrieval phases and return (evidence_blob_by_source, api_calls, info, cost).

    url mode → (optional Phase 1) + fetch + extract + search. The fetch+extract half is CACHED per
    (url, model, include_google): a re-run reuses the cached names (cost 0, no LLM) while the
    registry SEARCH still runs live — so you re-test the search layer for free. refresh=True bypasses
    the cache to re-run extraction. names mode → search only (no LLM, cost 0)."""
    from agent import EntityLookup
    agent = EntityLookup(config, progress_callback=None)
    model = config.get('model') or ''
    gintel, from_cache = {}, False
    if url:
        domain = _domain(url)
        cached = None if refresh else _extract_cache_get(url, model, bool(include_google))
        if cached:
            info = cached.get('info') or {}
            gintel = cached.get('gintel') or {}
            from_cache = True
        else:
            website_data = agent.fetch_website_data(url, domain)
            gintel = _google_intel_registries(agent, domain) if include_google else {}
            info = agent.extract_entities_with_llm(website_data, dict(gintel))
            info['entity_names'] = agent.deduplicate_names(info.get('entity_names') or [])
            _extract_cache_put(url, model, bool(include_google), {'info': info, 'gintel': gintel})
    else:
        domain = ''
        info = {'entity_names': list(names or []), 'short_names': [],
                'jurisdiction': (jurisdiction or 'unknown')}
    registries = agent.search_registries(info, domain)
    # the google-intel blocks are part of the evidence in prod, so include them (attribution too)
    merged = {**gintel, **registries}
    blob = {k: (v if isinstance(v, str) else json.dumps(v, default=str)) for k, v in merged.items()}
    it, ot = agent.total_input_tokens, agent.total_output_tokens
    ri, ro = _model_rates(model)
    cost = {'input_tokens': it, 'output_tokens': ot, 'from_cache': from_cache,
            'cost_usd': round(it * ri / 1_000_000 + ot * ro / 1_000_000, 4)}
    return blob, agent.tools.get_api_calls(), info, cost


# ══════════════════════════════════════════════════════════════════════════
# THE single Phase-1 content build — shared by the coverage test AND the model
# comparison. It produces BOTH the coverage evidence blob (for the grep) AND the
# exact analysis prompt (system,user) prod feeds the LLM, from one pipeline run,
# and caches the lot per case. Coverage greps `blob`; the model test feeds
# (system,user) to the models — they can never diverge again.
# ══════════════════════════════════════════════════════════════════════════
def _content_key(case) -> str:
    return json.dumps([case.get('url'), case.get('names'), case.get('jurisdiction'),
                       bool(case.get('include_google'))], default=str)


def grade_coverage(case: dict, blob: dict, api_calls: dict) -> dict:
    """Pure coverage grep verdict over an evidence blob (source→text). No I/O — shared by the
    coverage test and the model-comparison gate so they judge the SAME content."""
    full = "\n".join(f"[{k}]\n{v}" for k, v in blob.items())
    full_l = full.lower()
    throttled = any(m in full_l for m in _THROTTLE_MARKERS)

    def sources_for(s):
        sl = s.lower()
        return sorted({k.split(':')[0] for k, v in blob.items() if sl in v.lower()})

    checks, hard_fail, inconclusive = [], False, False
    for s in (case.get('expect') or []):
        srcs = sources_for(s)
        if srcs:
            checks.append({'kind': 'expect', 'text': s, 'status': 'pass', 'sources': srcs})
        elif throttled:
            checks.append({'kind': 'expect', 'text': s, 'status': 'inconclusive',
                           'note': 'a registry throttled/errored — not a confirmed miss'})
            inconclusive = True
        else:
            checks.append({'kind': 'expect', 'text': s, 'status': 'fail'})
            hard_fail = True
    for s in (case.get('forbid') or []):
        if s.lower() in full_l:
            checks.append({'kind': 'forbid', 'text': s, 'status': 'fail'}); hard_fail = True
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
    return {'status': status, 'checks': checks, 'throttled': throttled}


def build_content(config, case: dict, refresh: bool = False, progress=None) -> dict:
    """Run the pipeline once (fetch + optional Google intel + extract + search + cross-ref),
    then produce the coverage blob AND the analysis prompt from the same state; cache per case.
    Returns {system, user, blob, api_calls, info, cost, meta}. meta.coverage is the grep verdict."""
    from agent import EntityLookup
    cid, ck = case.get('id'), _content_key(case)
    if cid and not refresh:
        hit = _content_get(cid, ck)
        if hit:
            return hit
    agent = EntityLookup(config, progress_callback=progress)
    model = config.get('model') or ''
    url, names = case.get('url'), case.get('names')
    include_google = bool(case.get('include_google'))
    gintel = {}
    if url:
        domain = _domain(url)
        website_data = agent.fetch_website_data(url, domain)
        gintel = _google_intel_registries(agent, domain) if include_google else {}
        info = agent.extract_entities_with_llm(website_data, dict(gintel))
        info['entity_names'] = agent.deduplicate_names(info.get('entity_names') or [])
    else:
        domain = ''
        website_data = {'pages': {}, 'whois': 'Not available'}
        info = {'entity_names': list(names or []), 'short_names': [],
                'jurisdiction': (case.get('jurisdiction') or 'unknown')}
    registries = agent.search_registries(info, domain)
    cross = agent.cross_reference_sec_data(website_data, registries, info)
    if cross:
        registries['sec_cross_reference'] = cross
    merged = {**gintel, **registries}
    blob = {k: (v if isinstance(v, str) else json.dumps(v, default=str)) for k, v in merged.items()}
    system, user, sections = agent.build_analysis_messages(url or '', domain, website_data, info, merged)
    it, ot = agent.total_input_tokens, agent.total_output_tokens
    ri, ro = _model_rates(model)
    api_calls = agent.tools.get_api_calls()
    cov = grade_coverage(case, blob, api_calls)
    content = {
        'system': system, 'user': user, 'sections': sections,   # sections = expandable chunks for display
        'blob': blob, 'api_calls': api_calls, 'info': info,
        'from_cache': False,
        'cost': {'input_tokens': it, 'output_tokens': ot,
                 'cost_usd': round(it * ri / 1_000_000 + ot * ro / 1_000_000, 4)},
        'meta': {'mode': 'url' if url else 'names', 'registries': list(merged.keys()),
                 'extracted_names': info.get('entity_names'), 'jurisdiction': info.get('jurisdiction'),
                 'include_google': include_google, 'user_chars': len(user),
                 'coverage': {'status': cov['status'],
                              'missing': [c['text'] for c in cov['checks']
                                          if c['kind'] == 'expect' and c['status'] == 'fail']}},
    }
    if cid:
        _content_put(cid, ck, content)
    return content


# ── one case → structured result ──────────────────────────────────────────
def run_case(config, case: dict, refresh: bool = False) -> dict:
    """Build (or reuse) the case's Phase-1 content and grep it — the coverage test. Uses the SAME
    build_content the model comparison consumes, so the two can't diverge. refresh rebuilds."""
    try:
        content = build_content(config, case, refresh=refresh)
    except Exception as e:  # noqa: BLE001
        return {'case': case.get('name'), 'status': 'error', 'error': str(e), 'checks': [], 'cost_usd': 0}
    grade = grade_coverage(case, content['blob'], content['api_calls'])
    full = "\n".join(f"[{k}]\n{v}" for k, v in content['blob'].items())
    cost = content['cost']
    return {
        'case': case.get('name'),
        'status': grade['status'],
        'checks': grade['checks'],
        'api_calls': {k: v for k, v in content['api_calls'].items() if v},
        'cost_usd': cost['cost_usd'],
        'tokens': {'input': cost['input_tokens'], 'output': cost['output_tokens']},
        'from_cache': content.get('from_cache', False),
        'include_google': bool(case.get('include_google')),
        'mode': content['meta']['mode'],
        'extracted_names': content['meta']['extracted_names'],
        'jurisdiction': content['meta']['jurisdiction'],
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
                ALTER TABLE entity.coverage_cases
                    ADD COLUMN IF NOT EXISTS include_google boolean NOT NULL DEFAULT false;
                ALTER TABLE entity.coverage_cases ADD COLUMN IF NOT EXISTS last_result jsonb;
                ALTER TABLE entity.coverage_cases ADD COLUMN IF NOT EXISTS last_run_at timestamptz;
                -- extraction cache: (url, model, include_google) → {info, gintel}. Lets URL-mode
                -- re-runs skip fetch + the extraction LLM (free/fast) while still re-running the
                -- live registry search — refresh bypasses it to re-test extraction itself.
                CREATE TABLE IF NOT EXISTS entity.coverage_extract_cache (
                    url            text NOT NULL,
                    model          text NOT NULL,
                    include_google boolean NOT NULL DEFAULT false,
                    payload        jsonb NOT NULL,
                    created_at     timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (url, model, include_google)
                );
                -- the ONE Phase-1 content artifact per case (blob + analysis prompt), shared by
                -- the coverage test and the model comparison so they can't diverge.
                CREATE TABLE IF NOT EXISTS entity.phase1_content (
                    case_id     bigint PRIMARY KEY,
                    content_key text NOT NULL,
                    content     jsonb NOT NULL,
                    built_at    timestamptz NOT NULL DEFAULT now()
                );
            """)
        c.commit()
    _seed_if_empty()


def _extract_cache_get(url, model, include_google):
    if not enabled():
        return None
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("SELECT payload FROM entity.coverage_extract_cache "
                        "WHERE url=%s AND model=%s AND include_google=%s",
                        (url, model, include_google))
            r = cur.fetchone()
            return r[0] if r else None


def _extract_cache_put(url, model, include_google, payload):
    if not enabled():
        return
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.coverage_extract_cache (url, model, include_google, payload) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (url, model, include_google) "
                "DO UPDATE SET payload=EXCLUDED.payload, created_at=now()",
                (url, model, include_google, json.dumps(payload)))
        c.commit()


# ── shared Phase-1 content cache (the one artifact both tools consume) ────────
def _content_get(cid, ck):
    if not enabled():
        return None
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("SELECT content FROM entity.phase1_content WHERE case_id=%s AND content_key=%s",
                        (cid, ck))
            r = cur.fetchone()
            if not r:
                return None
            content = r[0]
            content['from_cache'] = True
            return content


def _content_put(cid, ck, content):
    if not enabled():
        return
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.phase1_content (case_id, content_key, content) VALUES (%s,%s,%s) "
                "ON CONFLICT (case_id) DO UPDATE SET content_key=EXCLUDED.content_key, "
                "content=EXCLUDED.content, built_at=now()",
                (cid, ck, json.dumps(content, default=str)))
        c.commit()


def content_cache_get(cid):
    """Read-only: the cached content for a case, or None (never triggers a build). For the
    matrix / page-load, which must not run the pipeline."""
    if not enabled() or not cid:
        return None
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("SELECT content FROM entity.phase1_content WHERE case_id=%s", (cid,))
            r = cur.fetchone()
            return r[0] if r else None


def _row(r):
    """dict-ify a case row, making it JSON-safe (timestamps → isoformat)."""
    d = dict(r)
    for k in ("created_at", "last_run_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


def save_last_result(cid: int, result: dict) -> None:
    """Persist a case's most recent run result so the page can show it on load."""
    if not enabled():
        return
    try:
        with closing(_conn()) as c:
            with c.cursor() as cur:
                cur.execute("UPDATE entity.coverage_cases SET last_result=%s, last_run_at=now() WHERE id=%s",
                            (json.dumps(result, default=str), cid))
            c.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[coverage] save_last_result failed: {e}")


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
                "(name,url,names,jurisdiction,expect,forbid,expect_in_source,max_calls,include_google) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (case.get('name') or 'unnamed', case.get('url'),
                 json.dumps(case.get('names') or None), case.get('jurisdiction'),
                 json.dumps(case.get('expect') or []), json.dumps(case.get('forbid') or []),
                 json.dumps(case.get('expect_in_source') or {}),
                 json.dumps(case.get('max_calls') or {}), bool(case.get('include_google'))))
            cid = cur.fetchone()[0]
        c.commit()
    return cid


def update_case(cid: int, case: dict) -> None:
    """Overwrite a case's fields in place (for the edit form)."""
    with closing(_conn()) as c:
        with c.cursor() as cur:
            # editing changes what pass/fail means, so the stored last_result is now stale — clear it
            cur.execute(
                "UPDATE entity.coverage_cases SET name=%s, url=%s, names=%s, jurisdiction=%s, "
                "expect=%s, forbid=%s, expect_in_source=%s, max_calls=%s, include_google=%s, "
                "last_result=NULL, last_run_at=NULL WHERE id=%s",
                (case.get('name') or 'unnamed', case.get('url'),
                 json.dumps(case.get('names') or None), case.get('jurisdiction'),
                 json.dumps(case.get('expect') or []), json.dumps(case.get('forbid') or []),
                 json.dumps(case.get('expect_in_source') or {}),
                 json.dumps(case.get('max_calls') or {}), bool(case.get('include_google')), cid))
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
    # German listed company via URL → exercises NorthData; the LEI is a stable anchor. Note the
    # site classifies as US (adidas America), yet the broad search still surfaces adidas AG from NorthData.
    {"name": "adidas.com → adidas AG (Germany · NorthData LEI)", "url": "http://www.adidas.com/",
     "expect": ["adidas AG", "549300JSX0Z4CW0V5023"],
     "expect_in_source": {"northdata": ["adidas AG"]}},
    # Finnish listed company (Oyj) via URL → exercises NorthData; LEI + Business ID as stable anchors.
    {"name": "scanfil.com → Scanfil Oyj (Finland · NorthData LEI)", "url": "https://www.scanfil.com/",
     "expect": ["Scanfil Oyj", "7437004XD6U0FFDCT507", "2422742-9"],
     "expect_in_source": {"northdata": ["Scanfil Oyj"]}},
]


def _seed_if_empty():
    try:
        if not list_cases():
            for c in _SEED:
                add_case(c)
    except Exception as e:  # noqa: BLE001
        print(f"[coverage] seed skipped: {e}")
