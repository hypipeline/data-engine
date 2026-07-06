"""
Python port of tests/test_sec_financials.php — SEC EDGAR XBRL financials.

Faithful port. The PHP suite does NOT construct any hardcoded XBRL/JSON fixture: every
assertion runs `secEdgarFinancials(<CIK>)`, which issues a live request to
https://data.sec.gov/api/xbrl/companyfacts/CIK<padded>.json and parses/formats the reply.
So all assertions are downstream of a live call and the module is marked `live` (skipped
unless RUN_LIVE is set).

PHP secEdgarFinancials -> Python sec_edgar_financials.
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
def t():
    return LookupTools(load_config(), progress_callback=None)


# 1. Apple — large company, standard US-GAAP tags
def test_apple(t):
    result = t.sec_edgar_financials("320193")
    assert len(result) > 0, "Returns data"
    assert "Apple" in result, "Has entity name"
    assert "Revenue" in result, "Has Revenue"
    assert "Net Income" in result, "Has Net Income"
    assert "Total Assets" in result, "Has Total Assets"
    assert "$" in result, "Has dollar amounts"


# 2. Goldman Sachs — financial company, uses RevenuesNetOfInterestExpense
def test_goldman_sachs(t):
    result = t.sec_edgar_financials("886982")
    assert len(result) > 0, "Returns data"
    assert "Goldman Sachs" in result, "Has entity name"
    assert "Revenue" in result, "Has Revenue"
    assert "Total Assets" in result, "Has Total Assets"


# 3. CrowdStrike — uses IncludingAssessedTax variant
def test_crowdstrike(t):
    result = t.sec_edgar_financials("1535527")
    assert len(result) > 0, "Returns data"
    assert "CROWDSTRIKE" in result, "Has entity name"
    assert "Revenue" in result, "Has Revenue"


# 4. Invalid CIK — should return empty
def test_invalid_cik(t):
    result = t.sec_edgar_financials("0")
    assert result == "", "Returns empty"


# 5. Non-existent CIK — should return empty
def test_nonexistent_cik(t):
    result = t.sec_edgar_financials("9999999999")
    assert result == "", "Returns empty"
