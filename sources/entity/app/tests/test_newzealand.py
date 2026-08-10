"""
Live test of the New Zealand (Companies Office / NZBN) registry mixin — the source added so
the pipeline can resolve NZ entities such as https://pioneercapital.co.nz/ to their register
entry (PIONEER CAPITAL MANAGEMENT LIMITED, NZ company no. 1585146).

Direct calls on LookupTools, in the same style as test_validation.py: each hits the live NZ
Companies Register search service, so the module is marked `live` and skipped unless RUN_LIVE
is set.
"""
import os

import pytest

from config import load_config
from tools import LookupTools

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="live network"),
]


@pytest.fixture(scope="module")
def tools():
    return LookupTools(load_config())


# ═══════════════════════════════════════════════════════════════════════════
#  search_newzealand — name → registry match (pioneercapital.co.nz)
# ═══════════════════════════════════════════════════════════════════════════

def test_search_pioneer_capital(tools):
    out = tools.search_newzealand("Pioneer Capital Management")
    assert "NZ Companies Office" in out                              # NZ registry block returned
    assert "PIONEER CAPITAL MANAGEMENT LIMITED" in out.upper()       # exact entity surfaced
    assert "1585146" in out                                          # NZ company number present
    assert "9429035043362" in out                                    # NZBN present
    assert "REGISTERED" in out.upper()                               # status is live


def test_search_enriches_best_match_with_ownership_counts(tools):
    # The top match is enriched via the entity-detail endpoint with director/shareholder counts
    # and any previous names (labels asserted, not brittle exact counts).
    out = tools.search_newzealand("Pioneer Capital Management")
    assert "Directors:" in out                                       # director count included
    assert "Shareholders:" in out                                    # shareholder count included
    assert "PIONEER CAPITAL PARTNERS" in out.upper()                 # previous name surfaced


def _bb_configured():
    return bool(load_config().get("browserbase_api_key") and load_config().get("browserbase_project_id"))


@pytest.mark.skipif(not _bb_configured(), reason="Browserbase not configured")
def test_search_integrates_ownership_on_exact_match():
    # Fresh instance so the one-shot ownership guard is unset: an exact-name search should fire the
    # Browserbase deep fetch and fold directors + shareholdings + percentages into the search output
    # that the registry phase feeds to the analysis LLM.
    fresh = LookupTools(load_config())
    out = fresh.search_newzealand("Pioneer Capital Management")
    assert "PIONEER CAPITAL MANAGEMENT LIMITED" in out.upper()       # summary line
    assert "Directors (" in out                                      # deep ownership fired within search
    assert "Shareholdings (total" in out
    assert "shares" in out and "%" in out                            # allocations with share counts + %
    assert "BARRETT" in out.upper()                                  # a named shareholder/director


@pytest.mark.skipif(not _bb_configured(), reason="Browserbase not configured")
def test_ownership_directors_and_shareholdings(tools):
    # Deep fetch via Browserbase of the single-page view: full director names + shareholder names + %.
    out = tools.newzealand_ownership("1585146")
    assert "Directors (" in out                                     # directors listed
    assert "BARRETT" in out.upper()                                 # founder director/shareholder (stable)
    assert "Shareholdings" in out                                   # shareholdings block present
    assert "shares" in out and "%" in out                           # allocations with share counts + percentages
    assert "PIONEER CAPITAL INVESTMENTS LIMITED" in out.upper()     # corporate shareholder surfaced


def test_detail_lookup_counts(tools):
    det = tools._nz_detail("1585146")
    assert det                                                       # detail resolves
    assert isinstance(det.get("director_count"), int)               # director count present
    assert isinstance(det.get("shareholder_count"), int)            # shareholder count present
    assert any("PIONEER CAPITAL PARTNERS" in (p or "").upper() for p in det.get("previous_names", []))


def test_search_ranks_exact_name_first(tools):
    # Even with the shorter brand ("Pioneer Capital"), the registered management company must rank
    # into the results rather than being buried under loose word-overlap hits.
    out = tools.search_newzealand("Pioneer Capital")
    assert "PIONEER CAPITAL MANAGEMENT LIMITED" in out.upper()


def test_search_no_match_is_clean(tools):
    out = tools.search_newzealand("Zzzq Nonexistent Holdings Xyztjq")
    assert ("No NZ Companies Office match" in out) or ("NZ Companies Office" in out)
    assert "Traceback" not in out


# ═══════════════════════════════════════════════════════════════════════════
#  lookup_newzealand_by_number — exact validation lookup
# ═══════════════════════════════════════════════════════════════════════════

def test_lookup_by_company_number(tools):
    rec = tools.lookup_newzealand_by_number("1585146")
    assert rec is not None                                           # exact number resolves
    assert "PIONEER CAPITAL MANAGEMENT" in (rec.get("name") or "").upper()
    assert rec.get("company_number") == "1585146"
    assert rec.get("nzbn") == "9429035043362"
    assert (rec.get("status") or "").upper() == "REGISTERED"
    assert rec.get("source") == "NZ Companies Office"


def test_lookup_by_nzbn(tools):
    rec = tools.lookup_newzealand_by_number("9429035043362")
    assert rec is not None                                           # NZBN also resolves
    assert rec.get("company_number") == "1585146"


def test_lookup_unknown_number_returns_none(tools):
    assert tools.lookup_newzealand_by_number("00000000") is None
    assert tools.lookup_newzealand_by_number("") is None


# ═══════════════════════════════════════════════════════════════════════════
#  Tool-use counting (count() + usage_summary) — no network for the pure test
# ═══════════════════════════════════════════════════════════════════════════

def test_count_mechanism_and_grouping():
    t = LookupTools(load_config())
    t.count("northdata", op="search")
    t.count("northdata", op="network", n=2)
    t.count("browserbase", n=3)
    t.count("nzco", op="search", cached=True)     # cache hit — not billed
    u = t.usage_summary()
    assert u["sources"]["northdata"] == 3          # 1 + 2, grouped as a source
    assert u["transport"]["browserbase"] == 3      # grouped as transport
    assert u["detail"]["northdata:network"] == 2   # per-operation detail
    assert u["cached"]["nzco"] == 1                # cache hits tracked separately
    assert u["total"] == 6                          # cached excluded from billed total


@pytest.mark.skipif(not _bb_configured(), reason="Browserbase not configured")
def test_nz_search_counts_sources_and_transport():
    t = LookupTools(load_config())
    t.search_newzealand("Pioneer Capital Management")
    u = t.usage_summary()
    assert u["sources"].get("nzco", 0) >= 2          # search + detail (+ ownership)
    assert u["transport"].get("browserbase", 0) >= 1 # single-page ownership render
    assert "nzco:ownership" in u["detail"]
