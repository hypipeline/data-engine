"""
Faithful port of ../../tests/test_northdata.php.

NorthData authentication + financial extraction.

PHP method mapping (PHP used ReflectionClass for the privates):
  getNorthdataAuthCookie      -> _get_northdata_auth_cookie()
  parseNorthdataHtml          -> _parse_northdata_html(html)
  extractNorthdataFinancials  -> _extract_northdata_financials(html)
  searchNorthdata             -> search_northdata(name)

Fixture-parse tests (PHP sections 2-5) are PURE — _parse_northdata_html and
_extract_northdata_financials make no network calls — so they run by default.
Login (section 1) and live search (section 6) are gated behind RUN_LIVE=1.

Fixtures live at sources/entity/tests/fixtures/ (shared with the PHP suite).
"""
import os
import pathlib

import pytest

from config import load_config
from tools import LookupTools

_FIXTURES = pathlib.Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"


@pytest.fixture(scope="module")
def tools():
    return LookupTools(load_config())


@pytest.fixture(scope="module")
def auth_html():
    return (_FIXTURES / "abca_group_auth.html").read_text()


@pytest.fixture(scope="module")
def noauth_html():
    return (_FIXTURES / "abca_group_noauth.html").read_text()


@pytest.fixture(scope="module")
def ltd_html():
    return (_FIXTURES / "abca_ltd_auth.html").read_text()


live = pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="live network")


# ══ 1. Authentication (LIVE) ════════════════════════════════════════════════

@pytest.mark.live
@live
def test_northdata_login(tools):
    cookie = tools._get_northdata_auth_cookie()
    assert cookie is not None                        # Returns auth cookie
    assert cookie.count(".") == 2                     # Cookie is JWT


# ══ 2. Parse financials — ABCA Systems Group Ltd (auth HTML) — PURE ═════════

def test_auth_fixture_loaded(auth_html):
    assert len(auth_html) > 50000                     # Auth fixture loaded


def test_parse_auth_html(tools, auth_html):
    result = tools._parse_northdata_html(auth_html)
    assert "Abca Systems Group Ltd" in result         # Has company name
    assert "Companies House 12500353" in result       # Has registry ID
    assert "### Financials" in result                 # Has Financials section
    assert "Revenue" in result                        # Has Revenue
    assert "£11M" in result                           # Has Revenue value £11M
    assert "Earnings" in result                       # Has Earnings
    assert "Total assets" in result                   # Has Total assets


# ══ 3. Parse financials — ABCA Systems Group Ltd (NO auth HTML) — PURE ══════

def test_noauth_fixture_loaded(noauth_html):
    assert len(noauth_html) > 50000                   # NoAuth fixture loaded


def test_parse_noauth_html(tools, noauth_html):
    result = tools._parse_northdata_html(noauth_html)
    assert "Abca Systems Group Ltd" in result         # Has company name
    # Without auth, no premium financials are expected (documents behaviour).


# ══ 4. Parse financials — Abca Systems Ltd (auth HTML) — PURE ═══════════════

def test_ltd_fixture_loaded(ltd_html):
    assert len(ltd_html) > 50000                      # Ltd fixture loaded


def test_parse_ltd_html(tools, ltd_html):
    result = tools._parse_northdata_html(ltd_html)
    assert "Abca Systems Ltd" in result               # Has company name
    assert "### Financials" in result                 # Has Financials section
    assert "Revenue" in result                        # Has Revenue
    assert "Earnings" in result                       # Has Earnings
    assert "Total assets" in result                   # Has Total assets


# ══ 5. extractNorthdataFinancials directly — PURE ═══════════════════════════

def test_extract_financials_auth(tools, auth_html):
    auth_fin = tools._extract_northdata_financials(auth_html)
    assert len(auth_fin) > 0                           # Auth financials not empty
    assert "Revenue" in auth_fin                       # Has Revenue row
    assert "Earnings" in auth_fin                       # Has Earnings row


def test_extract_financials_noauth(tools, noauth_html):
    # Empty is expected — premium data gated. Just ensure it does not raise.
    noauth_fin = tools._extract_northdata_financials(noauth_html)
    assert isinstance(noauth_fin, str)


# ══ 6. Live search with auth (LIVE) ═════════════════════════════════════════

@pytest.mark.live
@live
def test_search_northdata_live(tools):
    result = tools.search_northdata("ABCA Systems Group Ltd")
    assert "Abca Systems Group Ltd" in result          # Has company name
    assert "### Financials" in result                  # Has Financials section
    assert "Revenue" in result                         # Has Revenue
