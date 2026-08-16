"""
Deterministic (PURE) tests for the NorthData ownership-network parser.

Background: the network graph is fetched live via Browserbase (flaky — async render,
occasional empty "No network graph found") and THEN parsed. Those two concerns used to
live in one method (`northdata_network`), so the only way to check the parse was a live
fetch — which meant a render-timing miss looked identical to a parser bug. The parser is
now split out as `_parse_network_svg(html)` and exercised here against a real saved page,
with ZERO network I/O. If these fail, the parser is broken; if the live path fails but
these pass, the problem is the fetch/render, not the extraction.

Fixture: sources/entity/tests/fixtures/questglobal_network.html — a real NorthData entity
page saved from the browser for
  Quest Global Services PTE. Ltd., Singapore  (/_c5070753268498432)
This is the exact entity the branch-skip URL fix must land on (NOT the branch BR 999506259).
Every expected value below was read off that saved page.
"""
import pathlib

import pytest

from config import load_config
from tools import LookupTools

_FIXTURES = pathlib.Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"


@pytest.fixture(scope="module")
def tools():
    return LookupTools(load_config())


@pytest.fixture(scope="module")
def network_html():
    return (_FIXTURES / "questglobal_network.html").read_text()


@pytest.fixture(scope="module")
def parsed(tools, network_html):
    return tools._parse_network_svg(network_html, "https://www.northdata.com/_c5070753268498432")


# ── fixture sanity ──────────────────────────────────────────────────────────
def test_fixture_loaded(network_html):
    assert len(network_html) > 50000                              # real page loaded
    assert 'aria-label="Network"' in network_html                 # the graph SVG is present


# ── the parser must surface the ownership network from the real page ────────
def test_root_entity_identified(parsed):
    # the [ROOT] node must be Quest Global itself, not a parent or a branch
    assert "Entity: Quest Global Services PTE. Ltd. [ROOT]" in parsed
    assert "=== NorthData Network: Quest Global Services PTE. Ltd., Singapore ===" in parsed


def test_owned_by_section_present(parsed):
    # this is precisely what the pipeline was returning empty for (46-char miss)
    assert "OWNED BY (parent entities):" in parsed


@pytest.mark.parametrize("parent", [
    "Quest Global Services-NA Inc.",
    "Synapse Design Automation Inc.",
    "Quest Global Engineering Services Ltd.",
    "Quest Global Engineering Solutions PTE",   # truncated label on the graph
    "Agreeya Mobility India Ltd.",
])
def test_ultimate_parents_extracted(parsed, parent):
    assert parent in parsed


def test_ultimate_parent_relationship_labelled(parsed):
    assert "Ultimate parent" in parsed


def test_conclusion_points_to_parent(parsed):
    # ownership found → the tool should advise contracting with the parent, not conclude TopCo
    assert "has parent/owner entities above it" in parsed
    assert "ultimate parent/TopCo" not in parsed


def test_coverage_assertion_strings_present(parsed):
    # keep this test in lockstep with the coverage case:
    #   expect_in_source northdata_network ["OWNED BY", "Quest Global Engineering"]
    assert "OWNED BY" in parsed
    assert "Quest Global Engineering" in parsed


# ── negative: a page with no network graph must degrade cleanly, not crash ───
def test_no_network_graph_message(tools):
    out = tools._parse_network_svg("<html><body>no graph here</body></html>", "http://x")
    assert out == "No network graph found on this NorthData page."


# ══ ULTIMATE-PARENT RESOLVER (northdata_structure) — accuracy vs known truth ═══
# The flat parser reports 5 "ultimate parents" for Quest Global; the resolver must apply
# the data-old + arrowhead + top-with-live-chain logic and land on the ONE real answer.
import northdata_structure as ns  # noqa: E402


@pytest.fixture(scope="module")
def resolved(network_html):
    return ns.resolve(network_html)


def test_resolver_root_is_the_topco(resolved):
    # GROUND TRUTH (northdata.com): every 'Ultimate parent' arrow points DOWN from Services PTE
    # (arrowhead on the child), so Services PTE is ITSELF the ultimate parent / TopCo — the
    # Engineering Solutions page lists Services PTE as its ultimate parent. (The resolver used to
    # read these arrows backwards and wrongly named Engineering Solutions the parent.)
    assert resolved['has_network'] is True
    assert resolved['is_top_itself'] is True
    assert resolved['ultimate_parent'] is None


def test_resolver_former_edges_are_subsidiaries_not_parents(resolved):
    # The historical 'Ultimate parent' arrows (Services-NA, Agreeya) point DOWN from Services too →
    # they are FORMER SUBSIDIARIES, not former parents. Services PTE stays the TopCo.
    assert resolved['former_parents'] == []
    olds = [(s['owner'], s['owned']) for s in resolved['stakes'] if s['old']]
    assert any("Services PTE" in o and "Services-NA" in wd for o, wd in olds)
    assert any("Services PTE" in o and "Agreeya" in wd for o, wd in olds)


def test_resolver_synapse_is_a_subsidiary_not_a_parent(resolved):
    # With arrowheads read correctly Synapse is a CURRENT subsidiary of Services PTE (Services is
    # its ultimate parent), never a parent-candidate above the root.
    cur = [(s['owner'], s['owned']) for s in resolved['stakes'] if not s['old']]
    assert any("Services PTE" in o and "Synapse" in wd for o, wd in cur)
    assert not any("Synapse" in c['name'] for c in resolved['ultimate_candidates'])


def test_resolver_direction_owner_owns_subsidiary(resolved):
    # Arrowhead direction: the SG parent owns the small local subs, not the reverse.
    cur = [(s['owner'], s['owned']) for s in resolved['stakes'] if not s['old']]
    assert any("Engineering Solutions" in o and "España" in wd for o, wd in cur)
    assert any("Engineering Ltd" in o and "Poland" in wd for o, wd in cur)


def test_resolver_topco_negative_case():
    # A page whose root has no ultimate-parent pointer must resolve to TopCo, not invent a parent.
    svg = ('<svg aria-label="Network">'
           '<a class="node" data-id="1" data-text="Acme TopCo AG" data-root="x" '
           'data-description="Acme TopCo AG" data-warning=""></a>'
           '</svg>')
    res = ns.resolve(svg)
    assert res['has_network'] is True
    assert res['ultimate_parent'] is None
    assert res['is_top_itself'] is True
