"""Deterministic PARSER/RESOLVER unit tests for the NorthData ownership graph.

These own the parser layer (zero network I/O): each saved fixture is run through both the flat
parser and the direction-aware resolver and graded against known corporate-structure truth. They
share their case definitions with the harness module (northdata_cases.CASES) so there is ONE
source of truth for expectations — but the LIVE search→resolve→pick path is an INTEGRATION test
surfaced in the UI (/entity/tools/northdata), not here.
"""
import pytest

from config import load_config
import northdata_cases as nc

_CFG = load_config()


@pytest.mark.parametrize("case", nc.list_cases(), ids=lambda c: c["slug"])
def test_parser_case_grades_pass(case):
    r = nc.run_case(_CFG, case["slug"])
    assert not r.get("error"), r.get("error")
    failed = [c["label"] for c in r["checks"] if not c["ok"]]
    assert r["ok"], f"{case['slug']} failing checks: {failed}"


def test_all_fixtures_present():
    missing = [c["slug"] for c in nc.list_cases() if not c["fixture_exists"]]
    assert not missing, f"missing compact fixtures for: {missing}"


def test_adidas_resolves_topco_not_owned_by_subsidiary():
    """Regression: adidas AG is a TopCo — the resolver must NOT invert direction and call its
    subsidiaries 'parents' (the bug that fed the LLM 'contract with the Czech subsidiary')."""
    r = nc.run_case(_CFG, "adidas")
    assert r["resolved"]["is_top_itself"] is True
    assert r["resolved"]["ultimate_parent"] is None
