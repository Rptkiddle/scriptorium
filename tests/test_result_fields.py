"""Result fields: authors with given names, author count, date added,
abstracts on request, and the all-of author filter."""

import pytest
from qdrant_client import QdrantClient

from scriptorium.config import Config
from scriptorium.index import CATALOGUE, HubIndex, _catalogue_id
from scriptorium.names import author_names, authors_from_formatted, creator_keys
from scriptorium.search import (
    ABSTRACT_CHARS,
    ABSTRACTS_MAX,
    AUTHORS_SHOWN,
    HubSearch,
    build_filter,
)
from scriptorium.zotero import PaperRecord


def person(first, last, role="author"):
    return {"creatorType": role, "firstName": first, "lastName": last}


def test_author_names_keep_order_given_names_and_mark_roles():
    assert author_names([person("Damian", "Trilling"), person("Kasper", "Welbers", "editor"),
                         {"creatorType": "author", "name": "Pew Research Center"},
                         person("", "C. Kroon"), person("Ana", "Lee", "translator")]) == [
        "Damian Trilling", "Kasper Welbers (ed.)", "Pew Research Center", "C. Kroon",
        "Ana Lee (trans.)"]
    assert authors_from_formatted("Trilling, Kiddle & Welbers") == ["Trilling", "Kiddle", "Welbers"]


class StubEmbedder:
    def encode_documents(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def encode_item_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


def record(key, people, abstract="", added="2026-10-15T18:36:50Z"):
    return PaperRecord(item_key=key, version=1, item_type="journalArticle", title=key,
                       creators="x", year="2024", doi="", venue="V", citation_key=key.lower(),
                       date_added=added, pdf_key="", pdf_path="", pdf_status="no_pdf",
                       abstract=abstract, creator_keys=creator_keys(people),
                       authors=author_names(people))


@pytest.fixture
def setup(tmp_path):
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    client = QdrantClient(":memory:")
    hub = HubIndex(cfg, client=client, embedder=StubEmbedder())
    many = [person(f"Given{i}", f"Author{i}") for i in range(14)]
    hub.upsert_catalogue([
        record("BOTH", [person("Anne", "Kroon"), person("Damian", "Trilling")], abstract="x " * 1500),
        record("ONE", [person("Damian", "Trilling")], abstract="A short abstract."),
        record("MANY", many),
    ], {})
    return HubSearch(cfg, client=client, embedder=StubEmbedder()), client


def test_results_carry_first_authors_count_and_date_added(setup):
    search, client = setup
    hit = next(h for h in search.search_items("x", k=5) if h["item_key"] == "MANY")
    assert len(hit["authors"]) == AUTHORS_SHOWN and hit["author_count"] == 14
    assert hit["authors"][0] == "Given0 Author0" and hit["date_added"] == "2026-10-15"
    assert "abstract" not in hit
    stored = client.retrieve(CATALOGUE, ids=[_catalogue_id("MANY")], with_payload=True)[0]
    assert len(stored.payload["authors"]) == 14  # hub_item shows the full list
    row = next(i for i in search.list_items(None)["items"] if i["item_key"] == "ONE")
    assert row["authors"] == ["Damian Trilling"] and row["date_added"] == "2026-10-15"


def test_abstracts_on_request_cut_and_capped(setup):
    search, _ = setup
    hits = {h["item_key"]: h for h in search.search_items("x", k=5, abstracts=True)}
    assert hits["ONE"]["abstract"] == "A short abstract."
    assert len(hits["BOTH"]["abstract"]) <= ABSTRACT_CHARS + 6
    assert hits["BOTH"]["abstract"].endswith("[...]")
    page = search.list_items({"creators": ["trilling"]}, abstracts=True, limit=5)
    assert {i["item_key"]: i["abstract"][:7] for i in page["items"]} == {"BOTH": "x x x x", "ONE": "A short"}
    with pytest.raises(ValueError):
        search.search_items("x", k=ABSTRACTS_MAX + 1, abstracts=True)
    with pytest.raises(ValueError):
        search.list_items(None, abstracts=True)  # default limit 100 > 20


def test_creators_all_requires_every_author(setup):
    search, _ = setup
    any_of = search.list_items({"creators": ["Kroon", "Trilling"]})["total"]
    all_of = search.list_items({"creators_all": ["Anne Kroon", "Trilling, D."]})
    assert any_of == 2 and [i["item_key"] for i in all_of["items"]] == ["BOTH"]
    assert build_filter({"creators_all": ["kroon"]}, {"creators_all"}) is not None  # chunk paths too
