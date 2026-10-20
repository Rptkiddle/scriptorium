"""The catalogue: what is embedded, what is a filter, and the rebuild stamp."""

import json

import pytest
from qdrant_client import QdrantClient

from scriptorium.config import Config
from scriptorium.index import (
    CATALOGUE_DOC_VERSION,
    HubIndex,
    SyncState,
    catalogue_document,
    folder_paths,
)
from scriptorium.search import HubSearch
from scriptorium.zotero import PaperRecord

COLLS = [
    {"key": "CATJ", "name": "Journals", "parent": ""},
    {"key": "PUBS", "name": "Springer Nature", "parent": "CATJ"},
    {"key": "VENA", "name": "[✓] AI & Society", "parent": "PUBS"},
    {"key": "CATP", "name": "Preprints", "parent": ""},
    {"key": "PUBX", "name": "arXiv", "parent": "CATP"},
    {"key": "VENX", "name": "[✗] arXiv cs.XX", "parent": "PUBX"},
]


def record(key, title, abstract="", collections=(), creators="Trilling & Kiddle",
           year="2024", added="2026-10-15T18:00:00Z", item_type="journalArticle"):
    return PaperRecord(item_key=key, version=1, item_type=item_type, title=title,
                       creators=creators, year=year, doi=f"10.1/{key.lower()}", venue="AI & Society",
                       citation_key=key.lower(), date_added=added, pdf_key="", pdf_path="",
                       pdf_status="no_pdf", abstract=abstract, tags=["backfill-2020+"],
                       collections=list(collections))


class StubEmbedder:
    """Every text gets the same unit vector; ranking is not under test."""

    def encode_documents(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def encode_item_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


def test_catalogue_embeds_title_and_abstract_only():
    doc = catalogue_document(record("A", "Filter Bubbles", "An  abstract\nover lines."))
    assert doc == "Filter Bubbles\nAn abstract over lines."
    assert "Trilling" not in doc and "2024" not in doc and "backfill" not in doc
    assert catalogue_document(record("B", "Title Only")) == "Title Only"


def test_folder_paths_name_category_publisher_and_venue_without_marks():
    paths = folder_paths(COLLS)
    assert paths["VENA"] == ("Journals", "Springer Nature", "AI & Society")
    assert paths["VENX"] == ("Preprints", "arXiv", "arXiv cs.XX")
    assert paths["PUBS"] == ("Journals", "Springer Nature", "")


def test_catalogue_doc_stamp_round_trips_and_defaults_to_zero(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"library_version": 1, "items": {}, "failed": {}, "catalogue": {}}))
    state = SyncState.load(path)
    assert state.catalogue_doc == 0  # an older state file triggers a rebuild
    state.catalogue_doc = CATALOGUE_DOC_VERSION
    state.save(path)
    assert SyncState.load(path).catalogue_doc == CATALOGUE_DOC_VERSION


@pytest.fixture
def catalogue(tmp_path):
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    client = QdrantClient(":memory:")
    hub = HubIndex(cfg, client=client, embedder=StubEmbedder())
    hub.upsert_catalogue([
        record("A", "Paper A", collections=["VENA"], added="2026-10-01T00:00:00Z"),
        record("B", "Paper B", collections=["VENX"], creators="Welbers", item_type="preprint"),
        record("C", "Paper C", collections=[]),
    ], folder_paths(COLLS))
    return HubSearch(cfg, client=client, embedder=StubEmbedder())


def keys(search, filters):
    return sorted(r["item_key"] for r in search.search_items("anything", k=10, filters=filters))


def test_folder_filters_match_names_case_insensitively(catalogue):
    assert keys(catalogue, {"categories": ["journals"]}) == ["A"]
    assert keys(catalogue, {"publishers": ["arXiv"]}) == ["B"]
    assert keys(catalogue, {"venue_folders": ["ai & society"]}) == ["A"]
    assert keys(catalogue, {"creators": ["welbers"]}) == ["B"]


def test_added_since_filters_on_the_day_added(catalogue):
    assert keys(catalogue, {"added_since": "2026-10-15"}) == ["B", "C"]


def test_item_results_carry_venue_and_doi(catalogue):
    hit = next(r for r in catalogue.search_items("x", k=10) if r["item_key"] == "A")
    assert hit["venue"] == "AI & Society" and hit["doi"] == "10.1/a"
    assert hit["venue_folders"] == ["AI & Society"]


def test_facets_over_items_cover_every_catalogued_item(catalogue):
    assert catalogue.facets("categories", scope="items") == {"Journals": 1, "Preprints": 1}
    assert catalogue.facets("creators", "kid", scope="items") == {"kiddle": 2}
    assert catalogue.facets("item_types", scope="items") == {"journalArticle": 2, "preprint": 1}
    with pytest.raises(ValueError):
        catalogue.facets("categories")  # not a full-text field
    with pytest.raises(ValueError):
        catalogue.facets("years", scope="everything")
