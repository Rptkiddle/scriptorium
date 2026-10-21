"""Preprint and published versions: matching, Extra lines, and the add path."""

from typing import ClassVar

from scriptorium import additions
from scriptorium.versions import (
    VersionIndex,
    pair_action,
    rank,
    record_line,
    version_pairs,
    with_line,
)


def _item(key, item_type, title, surname, doi="", **extra_fields):
    return {"key": key, "itemType": item_type, "title": title, "DOI": doi,
            "creators": [{"creatorType": "author", "firstName": "A", "lastName": surname}],
            **extra_fields}


PUBLISHED = _item("PUB1", "journalArticle", "Echo Chambers Revisited", "Trilling",
                  "10.1000/journal.1")
PREPRINT = _item("PRE1", "preprint", "Echo chambers revisited!", "Trilling",
                 "10.31235/osf.io/abcde", repository="SocArXiv")


def test_title_and_first_author_pair_a_preprint_with_its_published_version():
    index = VersionIndex([PUBLISHED])
    match = index.other_version(PREPRINT)
    assert match.item is PUBLISHED and match.how == "title + first author"
    assert VersionIndex([PREPRINT]).other_version(PUBLISHED).item is PREPRINT


def test_two_published_items_with_the_same_title_and_author_are_not_versions():
    extension = _item("CONF", "conferencePaper", "Echo Chambers Revisited", "Trilling")
    assert VersionIndex([PUBLISHED]).other_version(extension) is None


def test_a_different_first_author_is_not_a_match():
    other = _item("PRE2", "preprint", "Echo Chambers Revisited", "Welbers")
    assert VersionIndex([PUBLISHED]).other_version(other) is None


def test_crossref_links_match_whatever_the_title():
    retitled = _item("PRE3", "preprint", "An early draft", "Kroon", "10.33774/apsa-1-v2")
    record = {"relation": {"is-preprint-of": [{"id-type": "doi", "id": "10.1000/JOURNAL.1"}]}}
    match = VersionIndex([PUBLISHED]).other_version(retitled, record)
    assert match.item is PUBLISHED and match.how == "crossref is-preprint-of"
    record = {"relation": {"has-preprint": [{"id-type": "doi", "id": "10.33774/apsa-1"},
                                            {"id-type": "doi", "id": "10.33774/apsa-1-v2"}]}}
    match = VersionIndex([retitled]).other_version(PUBLISHED, record)
    assert match.item is retitled and match.how == "crossref has-preprint"


def test_an_arxiv_doi_in_a_crossref_link_finds_the_arxiv_item():
    arxiv = _item("ARX", "preprint", "Something else", "Kroon", "10.48550/arXiv.2207.00868",
                  archiveID="arXiv:2207.00868", repository="arXiv")
    record = {"relation": {"has-preprint": [{"id-type": "doi", "id": "10.48550/arxiv.2207.00868"}]}}
    assert VersionIndex([arxiv]).other_version(PUBLISHED, record).item is arxiv


def test_a_published_version_is_preferred_over_a_preprint():
    index = VersionIndex([PREPRINT, PUBLISHED])
    newer = _item("PRE9", "preprint", "Echo Chambers Revisited", "Trilling", "10.1101/x")
    assert index.other_version(newer).item is PUBLISHED


def test_recorded_preprints_are_found_by_their_identifier():
    index = VersionIndex([{**PUBLISHED, "extra": "SocArXiv: 10.31235/osf.io/abcde"}])
    assert index.holder("10.31235/osf.io/abcde")["key"] == "PUB1"
    assert index.holder("10.1000/journal.1")["key"] == "PUB1"
    assert index.holder("10.1000/other") is None


def test_record_lines_use_the_repository_never_doi():
    assert record_line(PREPRINT) == ("doi:10.31235/osf.io/abcde",
                                     "SocArXiv: 10.31235/osf.io/abcde")
    arxiv = {"itemType": "preprint", "DOI": "10.48550/arXiv.2207.00868",
             "archiveID": "arXiv:2207.00868", "repository": "arXiv"}
    assert record_line(arxiv) == ("arxiv:2207.00868", "arXiv: 2207.00868")
    assert record_line({"itemType": "preprint", "DOI": "10.1000/x", "repository": "DOI"})[1] \
        == "Preprint: 10.1000/x"


def test_with_line_appends_once():
    extra = with_line("surveillance-why: fits", "doi:10.1000/x", "SocArXiv: 10.1000/x")
    assert extra == "surveillance-why: fits\nSocArXiv: 10.1000/x"
    assert with_line(extra, "doi:10.1000/x", "SocArXiv: 10.1000/x") == extra
    assert with_line("", "doi:10.1000/x", "SocArXiv: 10.1000/x") == "SocArXiv: 10.1000/x"


# --- add_papers --------------------------------------------------------------

class FakeReader:
    library: ClassVar[list] = []
    children_of: ClassVar[dict] = {}

    def __init__(self, config):
        pass

    def library_snapshot(self):
        return [dict(i) for i in self.library]

    def children(self, key):
        return self.children_of.get(key, [])


class FakeWriter:
    calls: ClassVar[list] = []

    def __init__(self, config, source="", reader=None):
        self.n = 0

    def create_items(self, items, collection_key="", taken=None):
        FakeWriter.calls.append(("create", [dict(i) for i in items]))
        keys = [f"NEW{len(FakeWriter.calls)}{i}" for i in range(len(items))]
        return {"created": keys, "citation_keys": {k: k.lower() for k in keys}, "failed": {}}

    def update_item(self, key, patch):
        FakeWriter.calls.append(("update", key, patch))

    def delete_item(self, key):
        FakeWriter.calls.append(("delete", key))


def _add(monkeypatch, library, records, entries, children=None, dry_run=False):
    FakeReader.library, FakeReader.children_of = library, children or {}
    FakeWriter.calls = []
    monkeypatch.setattr(additions, "ZoteroReader", FakeReader)
    monkeypatch.setattr(additions, "ZoteroWriter", FakeWriter)
    monkeypatch.setattr(additions, "http_session", lambda _: None)
    monkeypatch.setattr(additions.time, "sleep", lambda _: None)

    def fake_get(session, url, **params):
        if url.endswith("/works"):
            wanted = [f.removeprefix("doi:") for f in params["filter"].split(",")]
            return {"items": [records[d] for d in wanted if d in records]}
        raise RuntimeError("not found")

    monkeypatch.setattr(additions, "crossref_get", fake_get)
    return additions.add_papers(None, entries, dry_run=dry_run)


def _record(doi, kind, title, surname, relation=None, institution=""):
    record = {"DOI": doi, "type": kind, "title": [title],
              "author": [{"given": "A", "family": surname}], "relation": relation or {}}
    if institution:
        record["institution"] = [{"name": institution}]
    return record


def test_a_preprint_of_a_library_paper_is_recorded_not_created(monkeypatch):
    rec = _record("10.31235/osf.io/abcde", "posted-content", "Echo Chambers Revisited",
                  "Trilling", institution="SocArXiv")
    result = _add(monkeypatch, [{**PUBLISHED, "extra": "x: y"}],
                  {rec["DOI"]: rec}, [{"doi": rec["DOI"]}])
    assert result["created"] == {}
    assert result["versions"][rec["DOI"]] == {
        "case": "recorded", "matched_key": "PUB1", "how": "title + first author",
        "matched_title": "Echo Chambers Revisited"}
    assert FakeWriter.calls == [("update", "PUB1",
                                 {"extra": "x: y\nSocArXiv: 10.31235/osf.io/abcde"})]


def test_a_preprint_recorded_already_is_an_existing_item(monkeypatch):
    library = [{**PUBLISHED, "extra": "SocArXiv: 10.31235/osf.io/abcde"}]
    result = _add(monkeypatch, library, {}, [{"doi": "10.31235/osf.io/abcde"}])
    assert result["skipped_existing"] == ["10.31235/osf.io/abcde"]
    assert FakeWriter.calls == []


def test_a_published_paper_replaces_its_childless_preprint(monkeypatch):
    rec = _record("10.1000/journal.1", "journal-article", "Echo Chambers Revisited", "Trilling")
    result = _add(monkeypatch, [PREPRINT], {rec["DOI"]: rec}, [{"doi": rec["DOI"]}])
    (_, created), deleted = FakeWriter.calls
    assert created[0]["extra"] == "SocArXiv: 10.31235/osf.io/abcde"
    assert deleted == ("delete", "PRE1")
    assert result["versions"][rec["DOI"]]["preprint"] == "trashed"
    assert result["created"][rec["DOI"]]["key"].startswith("NEW")


def test_a_preprint_with_attachments_or_notes_is_kept(monkeypatch):
    rec = _record("10.1000/journal.1", "journal-article", "Echo Chambers Revisited", "Trilling")
    result = _add(monkeypatch, [PREPRINT], {rec["DOI"]: rec}, [{"doi": rec["DOI"]}],
                  children={"PRE1": [{"key": "N1", "item_type": "note"}]})
    assert [c[0] for c in FakeWriter.calls] == ["create"]
    assert result["versions"][rec["DOI"]]["preprint"] == "kept: it has note child items"


def test_a_second_preprint_is_recorded_on_the_first(monkeypatch):
    rec = _record("10.1101/2026.1", "posted-content", "Echo Chambers Revisited", "Trilling")
    result = _add(monkeypatch, [PREPRINT], {rec["DOI"]: rec}, [{"doi": rec["DOI"]}])
    assert result["versions"][rec["DOI"]]["matched_key"] == "PRE1"
    assert FakeWriter.calls[0][0] == "update"


def test_both_versions_in_one_call_create_only_the_published(monkeypatch):
    pub = _record("10.1000/journal.1", "journal-article", "Echo Chambers Revisited", "Trilling")
    pre = _record("10.31235/osf.io/abcde", "posted-content", "Echo Chambers Revisited",
                  "Trilling", institution="SocArXiv")
    result = _add(monkeypatch, [], {pub["DOI"]: pub, pre["DOI"]: pre},
                  [{"doi": pre["DOI"]}, {"doi": pub["DOI"]}])
    assert list(result["created"]) == [pub["DOI"]]
    (_, created), = FakeWriter.calls
    assert [i["DOI"] for i in created] == [pub["DOI"]]
    assert created[0]["extra"] == "SocArXiv: 10.31235/osf.io/abcde"
    assert result["versions"][pre["DOI"]]["matched_key"] == result["created"][pub["DOI"]]["key"]


def test_unrelated_papers_are_created_as_before(monkeypatch):
    rec = _record("10.1000/journal.2", "journal-article", "Something New", "Kroon")
    result = _add(monkeypatch, [PUBLISHED, PREPRINT], {rec["DOI"]: rec},
                  [{"doi": rec["DOI"]}, {"doi": "10.1000/JOURNAL.1"}])
    assert list(result["created"]) == [rec["DOI"]]
    assert result["skipped_existing"] == ["10.1000/journal.1"]
    assert result["versions"] == {}


def test_a_dry_run_writes_nothing_and_says_what_would_happen(monkeypatch):
    pub = _record("10.1000/journal.1", "journal-article", "Echo Chambers Revisited", "Trilling")
    pre = _record("10.1101/2026.1", "posted-content", "Unrelated Preprint", "Kroon")
    library = [PREPRINT, _item("OLD", "journalArticle", "Unrelated Preprint", "Kroon", "10.1000/j.9")]
    result = _add(
        monkeypatch, library, {pub["DOI"]: pub, pre["DOI"]: pre},
        [{"doi": pub["DOI"]}, {"doi": pre["DOI"]}], dry_run=True)
    assert FakeWriter.calls == []
    assert result["would_create"] == [pub["DOI"]]
    assert result["versions"][pub["DOI"]]["preprint"] == "would be trashed"
    assert result["versions"][pre["DOI"]]["matched_key"] == "OLD"


def test_a_preprint_without_an_identifier_is_kept(monkeypatch):
    philsci = _item("PHIL", "preprint", "Echo Chambers Revisited", "Trilling",
                    repository="PhilSci-Archive", url="https://philsci-archive.pitt.edu/1/")
    rec = _record("10.1000/journal.1", "journal-article", "Echo Chambers Revisited", "Trilling")
    result = _add(monkeypatch, [philsci], {rec["DOI"]: rec}, [{"doi": rec["DOI"]}])
    assert [c[0] for c in FakeWriter.calls] == ["create"]
    assert result["versions"][rec["DOI"]]["preprint"] == "kept: no identifier to record"


def test_a_dry_run_with_nothing_to_create_still_lists_would_create(monkeypatch):
    library = [{**PUBLISHED, "extra": "SocArXiv: 10.31235/osf.io/abcde"}]
    result = _add(monkeypatch, library, {}, [{"doi": "10.31235/osf.io/abcde"}], dry_run=True)
    assert result["would_create"] == []
    assert result["skipped_existing"] == ["10.31235/osf.io/abcde"]


# --- entries without a Crossref DOI -------------------------------------------

ARXIV_ITEM = {"itemType": "preprint", "title": "Echo Chambers Revisited",
              "creators": [{"creatorType": "author", "firstName": "A", "lastName": "Trilling"}],
              "repository": "arXiv", "archiveID": "arXiv:2603.13270",
              "DOI": "10.48550/arXiv.2603.13270", "url": "https://arxiv.org/abs/2603.13270"}


def test_an_arxiv_item_entry_of_a_library_paper_is_recorded_on_it(monkeypatch):
    result = _add(monkeypatch, [PUBLISHED], {}, [{"item": ARXIV_ITEM}])
    assert result["versions"]["arxiv:2603.13270"]["matched_key"] == "PUB1"
    assert FakeWriter.calls == [("update", "PUB1", {"extra": "arXiv: 2603.13270"})]


def test_item_entries_are_created_and_reported_by_their_identifier(monkeypatch):
    neurips = {"itemType": "conferencePaper", "title": "A NeurIPS Paper",
               "creators": [{"creatorType": "author", "lastName": "Kroon"}],
               "proceedingsTitle": "Advances in Neural Information Processing Systems",
               "url": "https://proceedings.neurips.cc/paper/2024/hash/abc-Abstract.html"}
    rec = _record("10.1000/journal.2", "journal-article", "Something New", "Welbers")
    result = _add(monkeypatch, [], {rec["DOI"]: rec},
                  [{"item": ARXIV_ITEM}, {"item": neurips}, {"doi": rec["DOI"]}])
    assert set(result["created"]) == {"arxiv:2603.13270", neurips["url"], rec["DOI"]}


def test_an_item_entry_whose_url_is_in_the_library_is_skipped(monkeypatch):
    library = [{**PUBLISHED, "url": "https://proceedings.neurips.cc/paper/2024/hash/abc-Abstract.html"}]
    entry = {"item": {"itemType": "conferencePaper", "title": "Other words",
                      "creators": [{"creatorType": "author", "lastName": "Kroon"}],
                      "url": "http://www.proceedings.neurips.cc/paper/2024/hash/abc-Abstract.html/"}}
    result = _add(monkeypatch, library, {}, [entry])
    assert result["skipped_existing"] == [entry["item"]["url"]]
    assert FakeWriter.calls == []


def test_an_item_entry_with_a_field_its_type_lacks_is_refused(monkeypatch):
    item = {**ARXIV_ITEM, "publicationTitle": "Not for preprints"}
    result = _add(monkeypatch, [], {}, [{"item": item}])
    assert "has no field publicationTitle" in result["failed"]["arxiv:2603.13270"]
    assert FakeWriter.calls == []
    no_author = {**ARXIV_ITEM, "creators": []}
    assert "needs a title" in _add(monkeypatch, [], {}, [{"item": no_author}])["failed"][
        "arxiv:2603.13270"]


# --- the library-wide check ---------------------------------------------------


def test_pairs_drop_a_preprint_for_its_published_version_and_rank_preprints():
    arxiv = _item("ARX", "preprint", "Echo Chambers Revisited", "Trilling",
                  "10.48550/arXiv.2603.13270", archiveID="arXiv:2603.13270")
    philsci = _item("PHIL", "preprint", "Model Anarchism", "Veit", archiveID="PhilSci:1")
    arxiv2 = _item("ARX2", "preprint", "Model anarchism", "Veit",
                   "10.48550/arXiv.2001.00001", archiveID="arXiv:2001.00001")
    assert [rank(i) for i in (PREPRINT, arxiv, philsci)] == [0, 1, 2]
    pairs = version_pairs([PUBLISHED, PREPRINT, philsci, arxiv2])
    assert [(k["key"], d["key"]) for k, d, _ in pairs] == [("PUB1", "PRE1"), ("ARX2", "PHIL")]


def test_two_published_items_are_never_paired():
    extension = _item("CONF", "conferencePaper", "Echo Chambers Revisited", "Trilling")
    assert version_pairs([PUBLISHED, extension]) == []


def test_pair_actions():
    assert pair_action(PUBLISHED, PREPRINT, []) == {
        "line": "SocArXiv: 10.31235/osf.io/abcde", "extra": "SocArXiv: 10.31235/osf.io/abcde",
        "trash": True, "action": "recorded; trashed"}
    kept = pair_action(PUBLISHED, PREPRINT, [{"item_type": "attachment"}])
    assert not kept["trash"] and kept["action"].endswith("it has attachment child items")
    already = pair_action({**PUBLISHED, "extra": "SocArXiv: 10.31235/osf.io/abcde"}, PREPRINT, [])
    assert already["extra"] is None and already["trash"]
    philsci = _item("PHIL", "preprint", "Echo Chambers Revisited", "Trilling", archiveID="PhilSci:1")
    flagged = pair_action(PUBLISHED, philsci, [])
    assert not flagged["trash"] and flagged["extra"] is None
    assert flagged["action"].startswith("flagged")


def test_an_item_entry_keeps_its_own_tags_and_collections(monkeypatch):
    item = {**ARXIV_ITEM, "tags": [{"tag": "own-work"}], "collections": ["COLL1"]}
    _add(monkeypatch, [], {}, [{"item": item, "tags": ["backfill-2020+", "own-work"]}])
    ((_, (created,)),) = FakeWriter.calls
    assert created["tags"] == [{"tag": "own-work"}, {"tag": "backfill-2020+"}]
    assert created["collections"] == ["COLL1"]
