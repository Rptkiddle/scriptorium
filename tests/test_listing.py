"""Listing and counting without a query (hub_list_items), facets within
filters, and the result caps."""

import pytest
from fastmcp.exceptions import ToolError
from qdrant_client import QdrantClient

from scriptorium import tools
from scriptorium.config import Config
from scriptorium.index import HubIndex, folder_paths
from scriptorium.search import LIST_LIMIT_MAX, HubSearch
from scriptorium.zotero import PaperRecord

COLLS = [
    {"key": "CATJ", "name": "Journals", "parent": ""},
    {"key": "PUBS", "name": "SAGE", "parent": "CATJ"},
    {"key": "NMS", "name": "[✓] New Media & Society", "parent": "PUBS"},
    {"key": "CATP", "name": "Preprints", "parent": ""},
    {"key": "PUBX", "name": "arXiv", "parent": "CATP"},
    {"key": "VENX", "name": "[✓] arXiv cs.SI", "parent": "PUBX"},
]


def record(key, title, year, coll="NMS", added="2026-10-14T10:00:00Z", creators="Trilling"):
    return PaperRecord(item_key=key, version=1, item_type="journalArticle", title=title,
                       creators=creators, year=year, doi="", venue="V", citation_key=key.lower(),
                       date_added=added, pdf_key="", pdf_path="", pdf_status="no_pdf",
                       collections=[coll])


class StubEmbedder:
    def encode_documents(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def encode_item_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


@pytest.fixture
def search(tmp_path):
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    client = QdrantClient(":memory:")
    hub = HubIndex(cfg, client=client, embedder=StubEmbedder())
    items = [record(f"S{i}", f"Same year paper {i}", "2022") for i in range(7)]
    items += [record("OLD", "An older paper", "2020"), record("NEW", "A newer paper", "2024",
                                                              added="2026-10-15T20:00:00Z"),
              record("PRE", "A preprint", "2023", coll="VENX", creators="Welbers")]
    hub.upsert_catalogue(items, folder_paths(COLLS))
    return HubSearch(cfg, client=client, embedder=StubEmbedder())


def keys(page):
    return [i["item_key"] for i in page["items"]]


def test_list_returns_total_page_and_next_offset(search):
    page = search.list_items({"venue_folders": ["New Media & Society"]}, limit=4)
    assert page["total"] == 9 and page["returned"] == 4 and page["next_offset"] == 4
    assert keys(page)[0] == "NEW" and set(page["items"][0]) == {
        "item_key", "citation_key", "citation", "item_type", "year", "venue_folders", "doi", "has_pdf",
        "authors", "author_count", "date_added"}
    last = search.list_items({"venue_folders": ["New Media & Society"]}, limit=4, offset=8)
    assert last["returned"] == 1 and last["next_offset"] is None and keys(last) == ["OLD"]


def test_count_only_and_filters_any_within_all_across(search):
    assert search.list_items(None, limit=0) == {
        "total": 10, "offset": 0, "returned": 0, "next_offset": None, "items": []}
    assert search.list_items({"categories": ["Preprints", "Journals"], "creators": ["welbers"]},
                             limit=0)["total"] == 1


def test_sorts_are_fixed_and_pages_never_overlap(search):
    whole = keys(search.list_items(None, limit=LIST_LIMIT_MAX))
    paged = []
    for offset in range(0, 10, 3):
        paged += keys(search.list_items(None, limit=3, offset=offset))
    assert paged == whole and len(set(paged)) == 10
    assert whole[:2] == ["NEW", "PRE"] and whole[-1] == "OLD"
    assert whole[2:9] == [f"S{i}" for i in range(7)]  # same year: by citation
    assert keys(search.list_items(None, sort="added", limit=1)) == ["NEW"]
    assert keys(search.list_items(None, sort="citation", limit=2)) == ["OLD", "S0"]


def test_list_rejects_bad_limits_and_sorts(search):
    with pytest.raises(ValueError):
        search.list_items(None, limit=LIST_LIMIT_MAX + 1)
    with pytest.raises(ValueError):
        search.list_items(None, sort="relevance")


def test_facets_count_within_filters_and_guard_fulltext(search):
    assert search.facets("years", scope="items",
                         filters={"venue_folders": ["New Media & Society"]}) == {
        "2020": 1, "2022": 7, "2024": 1}
    with pytest.raises(ValueError):
        search.facets("years", filters={"categories": ["Journals"]})


def test_search_items_k_is_capped_with_a_pointer_to_listing():
    with pytest.raises(ToolError, match="hub_list_items"):
        tools.hub_search_items("anything", k=500)


def test_unknown_filter_keys_are_rejected_with_the_valid_names(search):
    with pytest.raises(ValueError, match="unknown filter year_max, year_min.*year_from"):
        search.list_items({"year_min": 2020, "year_max": 2022})
    with pytest.raises(ValueError, match="unknown filter venues"):
        search.search_items("x", filters={"venues": ["V"]})
    with pytest.raises(ValueError, match="applies to item search"):
        search.search("x", filters={"categories": ["Journals"]})
