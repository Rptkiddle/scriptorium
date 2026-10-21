"""Preprint and published versions: matching, Extra lines, and the add path."""

from typing import ClassVar

from scriptorium import additions
from scriptorium.versions import VersionIndex, record_line, with_line


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


# --- add_by_doi --------------------------------------------------------------

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
    return additions.add_by_doi(None, entries, dry_run=dry_run)


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
