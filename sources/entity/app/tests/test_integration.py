"""
Faithful port of ../../php/tests/LookupTest.php — full EntityLookup pipeline.

  EntityLookup(config).run(url) -> {'report': ..., 'meta': ..., 'progress_log': ...}

Both cases drive the entire live pipeline (Google Intelligence, website fetch,
LLM extraction/analysis, registry searches, Browserbase), so both are gated
behind RUN_LIVE=1.
"""
import os

import pytest

from config import load_config
from agent import EntityLookup


live = pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="live network")


def _phase_nums(result):
    """The distinct phase_num markers emitted during run() (PHP's 8-phase pipeline)."""
    nums = set()
    for e in result.get("progress_log", []):
        detail = e.get("detail") or {}
        if isinstance(detail, dict) and "phase_num" in detail:
            nums.add(detail["phase_num"])
    return nums


def _all_log_text(result):
    """Flatten every progress-log message + expandable section content into one string. log_registry_result
    stores each registry's FULL result here, so registry evidence (incl. NZ ownership) is assertable
    without depending on the analysis LLM's wording."""
    buf = []
    for e in result.get("progress_log", []):
        buf.append(str(e.get("message") or ""))
        for sec in ((e.get("detail") or {}).get("sections") or []):
            buf.append(str(sec.get("content") or ""))
    return "\n".join(buf)


def _browserbase_configured():
    c = load_config()
    return bool(c.get("browserbase_api_key") and c.get("browserbase_project_id"))


# ══ kaincap.com — US entity, expected to resolve ════════════════════════════

@pytest.mark.live
@live
def test_full_lookup_kaincap():
    lookup = EntityLookup(load_config())
    result = lookup.run("https://www.kaincap.com/")

    report = result["report"]
    meta = result["meta"]

    assert "recommended_entity" in report                       # Has recommended_entity
    assert report.get("confidence") != "insufficient"           # Confidence is not insufficient

    entity = report.get("recommended_entity")
    if entity:
        name = (entity.get("legal_entity_name") or "").lower()
        assert "kain" in name                                   # Entity name contains "kain"
        assert entity.get("jurisdiction")                       # Has jurisdiction
        assert entity.get("source_url")                         # Has source URL

    assert meta["total_time_s"] < 300                           # Under 5 minutes
    assert report.get("evidence_forward")                       # Has forward evidence

    # The full pipeline is 8 phases; core phases 1-6 always run (7-8 conditional).
    nums = _phase_nums(result)
    assert nums, "no phase markers logged"
    assert {1, 2, 3, 4, 5, 6}.issubset(nums)                    # Core 8-phase pipeline ran
    assert nums.issubset({1, 2, 3, 4, 5, 6, 7, 8})


# ══ pioneercapital.co.nz — NZ entity, resolves via NZ Companies Office ══════

@pytest.mark.live
@live
def test_full_lookup_pioneer_capital_nz():
    lookup = EntityLookup(load_config())
    result = lookup.run("https://pioneercapital.co.nz/")

    report = result["report"]
    assert "recommended_entity" in report                       # Has recommended_entity
    assert report.get("confidence") != "insufficient"           # Confidence is not insufficient

    entity = report.get("recommended_entity")
    if entity:
        name = (entity.get("legal_entity_name") or "").lower()
        assert "pioneer capital" in name                        # Entity name contains "pioneer capital"
        assert "new zealand" in (entity.get("jurisdiction") or "").lower() \
            or "nz" in (entity.get("jurisdiction") or "").lower()   # NZ jurisdiction
        assert entity.get("source_url")                         # Has source URL
        # NZ company number (1585146) or NZBN (9429035043362) reached via the Companies Office
        rid = str(entity.get("registry_id") or "")
        assert rid in ("1585146", "9429035043362") or "companiesoffice.govt.nz" in (entity.get("source_url") or "")

    nums = _phase_nums(result)
    assert {1, 2, 3, 4, 5, 6}.issubset(nums)                    # Core 8-phase pipeline ran

    # The NZ Companies Office registry source was consulted in the pipeline...
    log_text = _all_log_text(result)
    assert "NZ Companies Office" in log_text                     # NZ register searched
    assert "1585146" in log_text                                 # NZ company number reached the evidence
    # ...and when Browserbase is available, the single-page ownership (directors + shareholdings +
    # percentages) was fetched and folded into the registry evidence the analysis LLM received.
    if _browserbase_configured():
        assert "Directors (" in log_text                        # director names present
        assert "Shareholdings" in log_text and "%" in log_text  # shareholder allocations + percentages


# ══ icenicapital.com — UK LLP, requires Browserbase (may be insufficient) ═══

@pytest.mark.live
@live
def test_full_lookup_iceni_capital():
    lookup = EntityLookup(load_config())
    result = lookup.run("http://www.icenicapital.com/")

    report = result["report"]
    entity = report.get("recommended_entity")

    # This site returns 502 and requires Browserbase rendering. If Browserbase
    # is rate-limited, the lookup returns insufficient.
    if entity:
        name = (entity.get("legal_entity_name") or "").lower()
        assert "iceni" in name                                  # Entity name contains "iceni"
        assert "llp" in name                                    # Entity is an LLP
    else:
        assert report.get("confidence") == "insufficient"       # Insufficient when site unreachable
