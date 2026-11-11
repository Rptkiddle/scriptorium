"""Admitting items: one item per publication, whichever way and however
often it arrives."""

import threading

import pytest

from scriptorium_server import db as database
from scriptorium_server.items import admit, get, holder, to_record


def record(item_type, title, surname, doi="", **fields):
    return {"itemType": item_type, "title": title, "DOI": doi, "date": "2026-03-01",
            "creators": [{"creatorType": "author", "firstName": "A", "lastName": surname}],
            **fields}


PUBLISHED = record("journalArticle", "Echo Chambers Revisited", "Trilling", "10.1000/journal.1",
                   publicationTitle="Journal of Tests", abstractNote="What we found.",
                   volume="12")
PREPRINT = record("preprint", "Echo chambers revisited!", "Trilling", "10.31235/osf.io/abcde",
                  repository="SocArXiv")
ARXIV = record("preprint", "Echo Chambers Revisited", "Trilling", archiveID="arXiv:2603.13270v2",
               repository="arXiv")


def count(db):
    return db.execute("SELECT count(*) AS n FROM items").fetchone()["n"]


def test_a_new_record_becomes_an_item_with_its_fields(db):
    key, created = admit(db, PUBLISHED, "venue")
    row = get(db, key)
    assert created and len(key) == 8
    assert (row["type"], row["title"], row["year"], row["doi"]) == (
        "journalArticle", "Echo Chambers Revisited", 2026, "10.1000/journal.1")
    assert row["abstract"] == "What we found." and row["category"] == "Journals"
    assert row["citation_key"] == "trillingEchoChambersRevisited2026"
    assert row["data"] == {"publicationTitle": "Journal of Tests", "volume": "12"}
    assert row["embedding"] is None and row["has_fulltext"] is False
    venue = db.execute("SELECT name FROM venues WHERE id = %s", (row["venue_id"],)).fetchone()
    assert venue["name"] == "Journal of Tests"
    assert to_record(row)["publicationTitle"] == "Journal of Tests"


def test_admit_twice_gives_one_item(db):
    first = admit(db, PUBLISHED, "venue")
    again = admit(db, {**PUBLISHED, "DOI": "https://doi.org/10.1000/JOURNAL.1"}, "activity")
    assert again == (first[0], False) and count(db) == 1


def test_two_sessions_admitting_at_once_give_one_item(db, empty_schema):
    """Review focus 1: a harvest and a member's lookup bring the same
    publication at the same moment."""
    db.execute("SELECT 1")  # the first session is inside a transaction of its own
    key, created = admit(db, PUBLISHED, "venue")  # admitted, not yet committed
    result = []

    def second_session():
        with database.connect(*empty_schema) as conn:
            result.append(admit(conn, PUBLISHED, "activity"))

    thread = threading.Thread(target=second_session)
    thread.start()
    thread.join(0.5)
    assert thread.is_alive() and result == []  # it waits for the first session
    db.commit()
    thread.join(10)
    assert created and result == [(key, False)] and count(db) == 1


def test_many_sessions_admitting_the_same_new_publication_give_one_item(db, empty_schema):
    start = threading.Barrier(6)
    results = []

    def session():
        with database.connect(*empty_schema) as conn:
            start.wait()
            results.append(admit(conn, ARXIV, "activity"))

    threads = [threading.Thread(target=session) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(results) == 6 and len({key for key, _ in results}) == 1
    assert sorted(created for _, created in results) == [False] * 5 + [True]
    assert count(db) == 1


def test_a_preprint_of_a_held_paper_is_recorded_on_it(db):
    key, _ = admit(db, PUBLISHED, "venue")
    assert admit(db, PREPRINT, "donation") == (key, False) and count(db) == 1
    row = get(db, key)
    assert row["other_ids"] == ["doi:10.31235/osf.io/abcde"]
    assert row["data"]["extra"] == "SocArXiv: 10.31235/osf.io/abcde"
    assert row["type"] == "journalArticle" and row["doi"] == "10.1000/journal.1"


def test_preprint_identifier_finds_published_item(db):
    key, _ = admit(db, PUBLISHED, "venue")
    admit(db, PREPRINT, "donation")
    admit(db, ARXIV, "donation")
    assert holder(db, ["doi:10.31235/osf.io/abcde"]) == key
    assert holder(db, ["arxiv:2603.13270"]) == key
    assert holder(db, ["doi:10.9999/unknown"]) is None
    # the same preprint arriving again is the published item, with nothing added twice
    assert admit(db, PREPRINT, "activity") == (key, False)
    assert get(db, key)["other_ids"] == ["arxiv:2603.13270", "doi:10.31235/osf.io/abcde"]


def test_a_published_version_replaces_its_preprint_and_keeps_its_identifier(db):
    key, _ = admit(db, PREPRINT, "donation")
    db.execute("UPDATE items SET embedding = %s WHERE key = %s",
               ("[" + ",".join(["0.1"] * 256) + "]", key))
    cited_as = get(db, key)["citation_key"]
    assert admit(db, PUBLISHED, "venue") == (key, False) and count(db) == 1
    row = get(db, key)
    assert (row["type"], row["doi"], row["abstract"]) == (
        "journalArticle", "10.1000/journal.1", "What we found.")
    assert row["other_ids"] == ["doi:10.31235/osf.io/abcde"]
    assert row["data"]["extra"] == "SocArXiv: 10.31235/osf.io/abcde"
    assert "repository" not in row["data"] and row["category"] == "Journals"
    assert row["citation_key"] == cited_as
    assert row["embedding"] is None  # to be embedded again from the new text
    assert holder(db, ["doi:10.31235/osf.io/abcde"]) == key


def test_a_second_preprint_is_recorded_on_the_first_and_carried_to_the_published(db):
    key, _ = admit(db, PREPRINT, "donation")
    assert admit(db, ARXIV, "donation") == (key, False)
    assert get(db, key)["other_ids"] == ["arxiv:2603.13270"]
    admit(db, PUBLISHED, "venue")
    assert get(db, key)["other_ids"] == ["arxiv:2603.13270", "doi:10.31235/osf.io/abcde"]


def test_provenance_kind_is_kept_from_first_admission(db):
    member = db.execute("INSERT INTO members (name, key_hash) VALUES ('m', 'h')"
                        " RETURNING id").fetchone()["id"]
    key, _ = admit(db, PREPRINT, "donation", member)
    admit(db, PREPRINT, "activity")
    admit(db, PUBLISHED, "venue")
    row = get(db, key)
    assert (row["provenance_kind"], row["provenance_member"]) == ("donation", member)


def test_registry_links_match_whatever_the_title(db):
    key, _ = admit(db, PUBLISHED, "venue")
    renamed = record("preprint", "An Earlier Working Title", "Trilling", "10.31235/osf.io/zzzzz",
                     repository="SocArXiv")
    relation = {"is-preprint-of": [{"id-type": "doi", "id": "10.1000/journal.1"}]}
    assert admit(db, renamed, "donation", relation=relation) == (key, False)
    assert admit(db, {**renamed, "DOI": "10.31235/osf.io/yyyyy"}, "donation")[1] is True


def test_two_published_items_with_one_title_are_two_items(db):
    admit(db, PUBLISHED, "venue")
    extension = record("conferencePaper", "Echo Chambers Revisited", "Trilling", "10.1145/1.2",
                       proceedingsTitle="Proceedings of Tests")
    key, created = admit(db, extension, "venue")
    assert created and count(db) == 2
    assert get(db, key)["citation_key"] == "trillingEchoChambersRevisited2026a"


def test_same_title_by_other_authors_is_another_item(db):
    admit(db, PUBLISHED, "venue")
    assert admit(db, record("preprint", "Echo Chambers Revisited", "Welbers",
                            "10.31235/osf.io/other"), "donation")[1] is True


def test_a_preprint_without_an_identifier_is_kept_as_its_own_item(db):
    admit(db, PUBLISHED, "venue")
    assert admit(db, record("preprint", "Echo Chambers Revisited", "Trilling"), "donation")[1]


def test_every_name_form_of_a_creator_is_indexed(db):
    paper = record("journalArticle", "Names", "van Atteveldt", "10.1000/n")
    paper["creators"].append({"creatorType": "author", "name": "Müller-Lüdenscheidt"})
    key, _ = admit(db, paper, "venue")
    rows = db.execute("SELECT name_key, position FROM item_creators WHERE item = %s"
                      " ORDER BY position, name_key", (key,)).fetchall()
    assert [(r["name_key"], r["position"]) for r in rows] == [
        ("atteveldt", 0), ("ludenscheidt", 1), ("muller", 1), ("muller-ludenscheidt", 1)]


def test_a_record_may_bring_its_own_keys_and_placement(db):
    seeded = {**PUBLISHED, "key": "ABCD2345", "citationKey": "trillingEcho2026"}
    key, _ = admit(db, seeded, "donation", publisher="Test Press", category="Journals")
    row = get(db, key)
    assert (key, row["citation_key"], row["publisher"]) == (
        "ABCD2345", "trillingEcho2026", "Test Press")
    venue = db.execute("SELECT publisher, category FROM venues").fetchone()
    assert (venue["publisher"], venue["category"]) == ("Test Press", "Journals")


@pytest.mark.parametrize("bad", [{"itemType": "journalArticle", "title": " "},
                                 {"title": "No type"}])
def test_a_record_without_type_or_title_is_refused(db, bad):
    with pytest.raises(ValueError, match="needs a type and a title"):
        admit(db, bad, "venue")
    assert count(db) == 0


def test_an_unknown_provenance_is_refused(db):
    with pytest.raises(ValueError, match="provenance must be"):
        admit(db, PUBLISHED, "import")
