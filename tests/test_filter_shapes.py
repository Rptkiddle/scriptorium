"""Filter value shapes, the facet cap and the per-tool reply limit."""

import asyncio
from types import SimpleNamespace

import pytest

from scriptorium import tools
from scriptorium.search import build_filter


@pytest.mark.parametrize("filters, message", [
    ({"creators": "Trilling"}, 'takes a list, e.g. "creators": \\["Trilling"\\]'),
    ({"creators": "Kroon, Trilling"}, "takes a list"),
    ({"venue_folders": "New Media & Society"}, "takes a list"),
    ({"years": ["twenty"]}, "whole numbers"),
    ({"year_from": True}, "whole numbers"),
    ({"has_pdf": "yes"}, "true or false"),
    ({"added_since": "15/10/2026"}, "YYYY-MM-DD"),
    ({"venue": ["V"]}, "one venue name as text"),
])
def test_wrong_shapes_are_refused_with_the_expected_form(filters, message):
    with pytest.raises(ValueError, match=message):
        build_filter(filters)


def test_year_text_is_read_as_numbers():
    flt = build_filter({"years": ["2020", 2021], "year_from": "2020"})
    values = [c.match.any for c in flt.must if c.key == "year_int" and c.match is not None]
    assert values == [[2020, 2021]]


def test_large_facets_without_contains_show_the_top_values(monkeypatch):
    counts = {f"name{i:03d}": i for i in range(300)}
    stub = SimpleNamespace(search=SimpleNamespace(facets=lambda *a: counts))
    monkeypatch.setattr(tools, "current", lambda: stub)
    out = tools.hub_facets("creators", scope="items")
    assert len(out) == tools.FACETS_TOP + 1 and out["_more"] == 100
    assert "name299" in out and "name000" not in out
    assert tools.hub_facets("creators", contains="name", scope="items") == counts
    assert tools.hub_facets("years", scope="items") == counts  # small fields are never cut


def test_search_tools_declare_a_higher_reply_limit():
    listed = {t.name: t for t in asyncio.run(tools.mcp.list_tools())}
    for name in ("hub_search_items", "hub_list_items", "hub_search_chunks", "hub_facets"):
        assert listed[name].meta == {"anthropic/maxResultSizeChars": 80_000}, name
