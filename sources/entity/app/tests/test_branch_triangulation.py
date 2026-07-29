"""
Regression tests for Bizapedia branch triangulation + trade-name/DBA owner resolution.

Guards the class of failure that broke herculite.com:
  - the 52-state sweep firing ~110 Bizapedia calls, hitting the rate limit and starving
    later searches (they returned false zeros);
  - the branch block swallowing the fictitious-name/DBA record that names the owner;
  - the owner resolving with no registry_id.

All assertions hit the live Bizapedia REST API (no LLM), so the module is `live` and skipped
unless RUN_LIVE is set:  RUN_LIVE=1 pytest sources/entity/app/tests/test_branch_triangulation.py
"""
import os

import pytest

from config import load_config
from tools import LookupTools

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="live network"),
]


@pytest.fixture()
def tools():
    return LookupTools(load_config())          # fresh per test → per-test api_calls counters


# ── herculite.com: 'Herculite Products' is a DBA of ABERDEEN ROAD COMPANY ──
def test_herculite_owner_linkage_detected(tools):
    _block, recs = tools.build_bizapedia_families("Herculite")
    assert recs, "short-name 'Herculite' search returned nothing (rate-limit starvation?)"
    hint_text, owners = tools.bizapedia_owner_hints(recs)
    assert "ABERDEEN ROAD COMPANY" in hint_text.upper()
    assert any("ABERDEEN ROAD COMPANY" in o.upper() for o in owners)


def test_herculite_owner_search_yields_pa_parent(tools):
    block, recs = tools.build_bizapedia_families("Aberdeen Road Company")
    assert recs
    # the domestic PA parent (the contracting entity) must be recoverable, with its file number
    assert any(r.get("FilingJurisdictionPostalAbbreviation") == "PA"
               and (r.get("FileNumber") or "") == "2887899" for r in recs)
    assert block and "2887899" in block and "PA" in block


def test_herculite_search_does_not_sweep(tools):
    """The primary-name search must not fire a per-state sweep (that caused the starvation).
    Foreign records here have a blank home jurisdiction → no triangulation → a single call."""
    tools.api_calls["bizapedia"] = 0
    tools.build_bizapedia_families("Herculite Products, Inc.")
    assert tools.api_calls["bizapedia"] <= 4, \
        f"expected a handful of calls, got {tools.api_calls['bizapedia']} (runaway sweep?)"


# ── Alianza: multiple branches all point HOME → the Delaware parent ──
def test_alianza_recommends_delaware_parent(tools):
    block, recs = tools.build_bizapedia_families("Alianza, LLC")
    assert block, "expected a branch-triangulation block for Alianza, LLC"
    assert 'home DE' in block or '(home DE)' in block
    assert "2987760" in block                  # the DE domestic parent file number
    assert tools.api_calls["bizapedia"] <= 5, \
        f"Alianza should resolve in a few calls, got {tools.api_calls['bizapedia']}"
