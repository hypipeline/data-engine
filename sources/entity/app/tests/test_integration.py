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
