"""Additions: Crossref record mapping, text cleaning, DOI normalisation."""

from scriptorium.additions import _clean, crossref_record_to_item
from scriptorium.apis import norm_doi, strip_jats
from scriptorium.citekeys import generate_citekey

FALLBACK = {"doi": "10.1000/x", "title": "Fallback Title",
            "authors": "Ada Lovelace, Alan Turing", "abstract": "fb abstract",
            "venue": "Fallback Venue", "year": 2020}


def test_norm_doi():
    assert norm_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert norm_doi("http://dx.doi.org/10.1000/abc") == "10.1000/abc"
    assert norm_doi("  10.1000/Abc ") == "10.1000/abc"
    assert norm_doi("") == ""


def test_clean_strips_jats_and_html():
    assert _clean("<jats:p>Text <i>markup</i></jats:p>") == "Text markup"
    assert _clean("") == ""


def test_strip_jats_plain_text_untouched():
    assert strip_jats("No markup here.") == "No markup here."


def test_record_mapping_full():
    record = {
        "DOI": "10.1000/X", "title": ["A <i>Real</i> Title"],
        "author": [{"given": "Grace", "family": "Hopper"},
                   {"family": "Curie"}],
        "issued": {"date-parts": [[2026, 9, 3]]},
        "container-title": ["Journal of Tests"],
        "volume": "12", "issue": "3", "page": "1-10",
        "abstract": "<jats:p>An abstract.</jats:p>",
        "ISSN": ["1111-2222", "3333-4444", "5555-6666"],
    }
    item = crossref_record_to_item(record, FALLBACK)
    assert item["title"] == "A Real Title"
    assert item["creators"][0] == {"creatorType": "author",
                                   "firstName": "Grace", "lastName": "Hopper"}
    assert item["date"] == "2026-09-03"
    assert item["DOI"] == "10.1000/x"
    assert item["abstractNote"] == "An abstract."
    assert item["ISSN"] == "1111-2222, 3333-4444"
    assert item["url"] == "https://doi.org/10.1000/x"


def test_record_mapping_year_only_date():
    item = crossref_record_to_item(
        {"DOI": "10.1/y", "title": ["T"], "issued": {"date-parts": [[2024]]}},
        FALLBACK)
    assert item["date"] == "2024"


def test_empty_crossref_abstract_falls_back():
    # Some publishers deposit an abstract element with no text.
    item = crossref_record_to_item(
        {"DOI": "10.1/z", "title": ["T"], "abstract": "<jats:p/>"}, FALLBACK)
    assert item["abstractNote"] == "fb abstract"


def test_record_mapping_fallbacks():
    item = crossref_record_to_item(None, FALLBACK)
    assert item["title"] == "Fallback Title"
    assert [(c["firstName"], c["lastName"]) for c in item["creators"]] == [
        ("Ada", "Lovelace"), ("Alan", "Turing")]
    assert item["date"] == "2020"
    assert item["publicationTitle"] == "Fallback Venue"
    assert item["DOI"] == "10.1000/x"


def test_citekey_collision_suffixes():
    data = {"creators": [{"creatorType": "author", "lastName": "Hopper"}],
            "title": "Compiling Very Fast Programs", "date": "2026"}
    taken: set = set()
    first = generate_citekey(data, taken)
    taken.add(first)
    second = generate_citekey(data, taken)
    assert first != second
    assert second.startswith(first)



# -- item types, key pairing, citation-key uniqueness, bridge timeouts -------

import urllib.error  # noqa: E402

import pytest  # noqa: E402

from scriptorium import additions  # noqa: E402
from scriptorium.citekeys import unique_key  # noqa: E402
from scriptorium.zotero_bridge import (  # noqa: E402
    BridgeTimeout,
    BridgeUnavailable,
    ZoteroBridge,
)


def test_proceedings_article_becomes_conference_paper():
    item = crossref_record_to_item(
        {"type": "proceedings-article", "DOI": "10.1/c", "title": ["P"],
         "container-title": ["Proc. of CHI"], "event": {"name": "CHI 2026"},
         "ISBN": ["978-1"], "issue": ""}, FALLBACK)
    assert item["itemType"] == "conferencePaper"
    assert item["proceedingsTitle"] == "Proc. of CHI"
    assert item["conferenceName"] == "CHI 2026"
    assert "publicationTitle" not in item and "issue" not in item


def test_caller_item_type_overrides_crossref_and_names_the_conference():
    # Crossref files AAAI papers as journal articles and LNCS papers as
    # book chapters; the caller knows they are conference papers.
    for crossref_type in ("journal-article", "book-chapter"):
        item = crossref_record_to_item(
            {"type": crossref_type, "DOI": "10.1/a", "title": ["P"],
             "container-title": ["Proceedings of the AAAI Conference"], "issue": "5"},
            {**FALLBACK, "item_type": "conferencePaper", "conference_name": "AAAI"})
        assert item["itemType"] == "conferencePaper"
        assert item["proceedingsTitle"] == "Proceedings of the AAAI Conference"
        assert item["conferenceName"] == "AAAI" and item["issue"] == "5"
        assert "publicationTitle" not in item and "bookTitle" not in item
    item = crossref_record_to_item(
        {"type": "proceedings-article", "event": {"name": "CHI 2026"}},
        {**FALLBACK, "item_type": "conferencePaper", "conference_name": "CHI"})
    assert item["conferenceName"] == "CHI 2026"


def test_posted_content_becomes_preprint_without_journal_fields():
    item = crossref_record_to_item(
        {"type": "posted-content", "DOI": "10.1/p", "title": ["Pre"], "volume": "3",
         "institution": [{"name": "SocArXiv"}], "ISSN": ["1234"]}, FALLBACK)
    assert item["itemType"] == "preprint"
    assert item["repository"] == "SocArXiv"
    assert not {"volume", "ISSN", "publicationTitle", "pages"} & set(item)
    assert item["DOI"] == "10.1/p"


def test_fallback_authors_accept_a_list():
    item = crossref_record_to_item(None, {**FALLBACK, "authors": ["Grace Hopper", "Plato"]})
    assert item["creators"][0]["lastName"] == "Hopper"
    assert item["creators"][1] == {"creatorType": "author", "name": "Plato"}


def test_unique_key_goes_past_z():
    taken = {"k"} | {"k" + c for c in "abcdefghijklmnopqrstuvwxyz"}
    assert unique_key("k", taken) == "kaa"


class _FakeWriter:
    """The bridge reports successes in order and failures by chunk index."""

    def __init__(self, config, source="", reader=None):
        pass

    def create_items(self, items, collection_key="", taken=None):
        return {"created": ["KEY_A", "KEY_C"], "failed": {"1": "bad field"},
                "citation_keys": {"KEY_A": "ckA", "KEY_C": "ckC"}}


def test_created_keys_pair_with_the_right_dois(monkeypatch):
    class _Reader:
        def __init__(self, config):
            pass

        def library_snapshot(self):
            return []

    monkeypatch.setattr(additions, "ZoteroReader", _Reader)
    monkeypatch.setattr(additions, "http_session", lambda agent: None)
    monkeypatch.setattr(additions, "crossref_get", lambda *a, **k: {"items": [
        {"DOI": d, "title": [d]} for d in ("10.1/a", "10.1/b", "10.1/c")]})
    monkeypatch.setattr(additions, "ZoteroWriter", _FakeWriter)
    monkeypatch.setattr(additions.time, "sleep", lambda s: None)
    result = additions.add_papers(None, [{"doi": d} for d in ("10.1/a", "10.1/b", "10.1/c")])
    assert result["created"]["10.1/a"]["key"] == "KEY_A"
    assert result["created"]["10.1/a"]["citation_key"] == "ckA"
    assert result["created"]["10.1/c"]["key"] == "KEY_C"
    assert result["failed"] == {"10.1/b": "bad field"}


def _raising(exc):
    def urlopen(*a, **k):
        raise exc
    return urlopen


def test_bridge_timeout_is_not_reported_as_unavailable(monkeypatch):
    bridge = ZoteroBridge()
    monkeypatch.setattr("urllib.request.urlopen", _raising(TimeoutError("timed out")))
    with pytest.raises(BridgeTimeout):
        bridge.ping()
    monkeypatch.setattr("urllib.request.urlopen",
                        _raising(urllib.error.URLError(ConnectionRefusedError())))
    with pytest.raises(BridgeUnavailable):
        bridge.ping()
