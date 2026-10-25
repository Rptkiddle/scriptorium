"""Author name keys: one author is one filter value, however the name is
stored or typed."""


from scriptorium.config import Config
from scriptorium.index import HubIndex
from scriptorium.names import creator_keys, keys_from_formatted, query_key
from scriptorium.search import HubSearch
from scriptorium.tei import Passage
from scriptorium.zotero import PaperRecord


def person(last, first=""):
    return {"creatorType": "author", "lastName": last, "firstName": first}


def test_creator_keys_for_each_stored_pattern():
    assert creator_keys([person("van Atteveldt", "Wouter")]) == ["atteveldt"]
    assert creator_keys([person("Atteveldt", "Wouter van")]) == ["atteveldt"]
    assert creator_keys([person("Wouter van Atteveldt")]) == ["atteveldt", "wouter"]
    assert creator_keys([person("Damian Trilling")]) == ["damian", "trilling"]
    assert creator_keys([person("C. Kroon")]) == ["kroon"]
    assert creator_keys([person("Flöck", "Fabian")]) == ["flock"]
    assert creator_keys([person("García Márquez", "Gabriel")]) == ["garcia", "marquez"]
    assert creator_keys([person("Müller-Lüdenscheidt", "A.")]) == [
        "ludenscheidt", "muller", "muller-ludenscheidt"]
    assert creator_keys([person("Le", "Thi")]) == ["le"]
    assert creator_keys([person("Pham Huong Anh")]) == ["anh", "huong", "pham"]
    assert creator_keys([{"name": 'Fr{\\"o}hling, Leon'}]) == ["frohling", "leon"]
    assert keys_from_formatted("van Atteveldt, Kiddle & Welbers") == ["atteveldt", "kiddle", "welbers"]


def test_query_key_reduces_any_typed_form_to_one_key():
    for typed in ("van Atteveldt", "Van Atteveldt", "Wouter van Atteveldt", "Atteveldt, W.",
                  "atteveldt", "W. van Atteveldt"):
        assert query_key(typed) == "atteveldt", typed
    assert query_key("Flöck") == "flock" and query_key("Le") == "le"
    assert query_key("Müller-Lüdenscheidt") == "muller-ludenscheidt"
    assert query_key("Gabriel García Márquez") == "marquez"


class StubEmbedder:
    def encode_documents(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def encode_item_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]

    def encode_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


def record(key, creators):
    return PaperRecord(item_key=key, version=1, item_type="journalArticle", title=key,
                       creators="x", year="2024", doi="", venue="V", citation_key=key.lower(),
                       date_added="", pdf_key="", pdf_path="", pdf_status="no_pdf",
                       creator_keys=creator_keys(creators))


def test_four_stored_forms_match_one_filter_and_one_facet_value(tmp_path):
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    hub = HubIndex(cfg, embedder=StubEmbedder())
    hub.upsert_catalogue([
        record("A", [person("van Atteveldt", "Wouter")]),
        record("B", [person("Atteveldt", "Wouter van")]),
        record("C", [person("Wouter van Atteveldt")]),
        record("D", [person("Van Atteveldt", "W.")]),
        record("E", [person("Trilling", "Damian")]),
    ], {})
    search = HubSearch(cfg, hub=hub, embedder=StubEmbedder())
    for typed in ("van Atteveldt", "Atteveldt, W.", "Wouter van Atteveldt"):
        page = search.list_items({"creators": [typed]})
        assert sorted(i["item_key"] for i in page["items"]) == ["A", "B", "C", "D"], typed
    assert search.facets("creators", "atteveldt", scope="items") == {"atteveldt": 4}
    assert search.facets("creators", "Van Atteveldt", scope="items") == {"atteveldt": 4}


def test_chunks_read_their_metadata_from_the_catalogue(tmp_path):
    """A metadata edit changes one catalogue row: chunk search then shows the
    new citation key and filters on the new author keys, with the chunks
    themselves untouched."""
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    hub = HubIndex(cfg, embedder=StubEmbedder())
    old = PaperRecord(item_key="P", version=1, item_type="journalArticle", title="T",
                      creators="Wouter van Atteveldt", year="2024", doi="", venue="V",
                      citation_key="oldkey", date_added="", pdf_key="", pdf_path="",
                      pdf_status="ok")
    hub.upsert_catalogue([old], {})
    chunks = [Passage(text=f"t{i}", embed_text=f"t{i}", pages=f"{i}-{i + 1}", part="body")
              for i in range(3)]
    hub.add_paper(old, chunks, "h")
    search = HubSearch(cfg, hub=hub, embedder=StubEmbedder())
    assert {r.name for r in search.search("q", k=5)} == {
        "oldkey pages 0-1", "oldkey pages 1-2", "oldkey pages 2-3"}
    new = PaperRecord(**{**old.__dict__, "version": 2, "citation_key": "newkey",
                         "creator_keys": creator_keys([person("van Atteveldt", "Wouter")])})
    hub.upsert_catalogue([new], {})
    assert hub.update_metadata(new) == 3
    hits = search.search("q", k=5, filters={"creators": ["van Atteveldt"]})
    assert sorted(r.name for r in hits) == [
        "newkey pages 0-1", "newkey pages 1-2", "newkey pages 2-3"]
    assert all(r.citation_key == "newkey" for r in hits)
