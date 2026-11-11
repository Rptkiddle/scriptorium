"""The seed import, from a small index in the personal version's layout and
a records file as the export writes it."""

import json
import sqlite3
import struct

import pytest

from scriptorium_server import jobs, members
from scriptorium_server.embedding import DIM, MODEL
from scriptorium_server.seed import export_records, seed

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE items(item_key TEXT PRIMARY KEY, version INTEGER NOT NULL, has_pdf INTEGER NOT NULL,
    citation_key TEXT NOT NULL DEFAULT '', item_type TEXT NOT NULL DEFAULT '', year_int INTEGER,
    venue_lc TEXT NOT NULL DEFAULT '', added_ts INTEGER, title_norm TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL, vec BLOB NOT NULL);
CREATE TABLE chunks(id INTEGER PRIMARY KEY, item_key TEXT NOT NULL, chunk_index INTEGER NOT NULL,
    part TEXT NOT NULL DEFAULT 'body', pages TEXT NOT NULL, text TEXT NOT NULL,
    artefact INTEGER NOT NULL DEFAULT 0, vec BLOB, UNIQUE(item_key, chunk_index));
CREATE TABLE papers(item_key TEXT PRIMARY KEY, version INTEGER NOT NULL,
    content_hash TEXT NOT NULL, chunks INTEGER NOT NULL, parser INTEGER, meta INTEGER);
"""


def blob(seed_value: float) -> bytes:
    return struct.pack(f"<{DIM}e", *[(seed_value + i) / 1000 for i in range(DIM)])


def payload(key, item_type, title, doi="", **more):
    return json.dumps({
        "item_key": key, "item_type": item_type, "title": title, "year": "2025", "doi": doi,
        "venue": "Journal of Tests", "abstract": "What we found.",
        "authors": ["Ada Lovelace", "Plato"], "tags": ["own-work"], "collections": ["COLL1"],
        "categories": ["Journals"], "publishers": ["Test Press"],
        "venue_folders": ["Journal of Tests (JoT)"],
        "identifiers": [f"doi:{doi}"] if doi else [], "preprint_identifiers": [], **more})


@pytest.fixture
def index(tmp_path):
    path = tmp_path / "index.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("embedding_model", MODEL), ("embedding_dim", str(DIM)), ("embedding_revision", "rev-1")])
    rows = [
        ("AAAA2222", "lovelaceNotes2025", payload("AAAA2222", "journalArticle", "Notes on the Engine", "10.1000/a"), blob(1)),
        ("BBBB3333", "platoForms2025", payload("BBBB3333", "journalArticle", "On Forms", "10.1000/b",
                                              preprint_identifiers=["arxiv:2501.00001"]), blob(2)),
        ("CCCC4444", "", payload("CCCC4444", "presentation", "A Talk Without Identifier"), blob(3)),
    ]
    con.executemany("INSERT INTO items (item_key, version, has_pdf, citation_key, payload, vec)"
                    " VALUES (?, 1, 1, ?, ?, ?)", rows)
    con.executemany("INSERT INTO chunks (item_key, chunk_index, part, pages, text, artefact, vec)"
                    " VALUES ('AAAA2222', ?, ?, ?, ?, ?, ?)", [
        (0, "body", "1", "First passage.", 0, blob(10)),
        (1, "body", "1-2", "1.1 0.1 2.2 0.2", 1, None),
        (2, "footnote", "2", "A footnote.", 0, blob(12))])
    con.execute("INSERT INTO papers VALUES ('AAAA2222', 1, 'md5-of-a', 3, 3, 3)")
    con.commit()
    con.close()
    return path


@pytest.fixture
def records(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_text(json.dumps({
        "key": "AAAA2222", "itemType": "journalArticle", "title": "Notes on the Engine",
        "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
                     {"creatorType": "author", "name": "Plato"}],
        "date": "2025-03-04", "DOI": "10.1000/a", "volume": "12", "pages": "1-20",
        "publicationTitle": "Journal of Tests", "abstractNote": "What we found.",
        "dateAdded": "2026-10-22T09:24:34Z"}) + "\n")
    return path


@pytest.fixture
def member(db):
    members.add(db, "ada")
    return "ada"


def table_count(db, table):
    return db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_seed_counts_match_source(db, index, records, member, tmp_path):
    counts = seed(db, index, member, records, tmp_path / "runs")
    assert (counts["source"], counts["created"], counts["present"], counts["collisions"]) == (3, 3, 0, [])
    assert (counts["from_records"], counts["from_index"]) == (1, 2)
    assert (counts["fulltexts"], counts["passages"]) == (1, 3)
    assert (table_count(db, "items"), table_count(db, "passages")) == (3, 3)
    rows = db.execute("SELECT key, provenance_kind, has_fulltext, category, publisher FROM items"
                      " ORDER BY key").fetchall()
    assert [(r["key"], r["provenance_kind"], r["has_fulltext"]) for r in rows] == [
        ("AAAA2222", "donation", True), ("BBBB3333", "donation", False),
        ("CCCC4444", "donation", False)]
    assert {(r["category"], r["publisher"]) for r in rows} == {("Journals", "Test Press")}
    assert db.execute("SELECT name FROM venues").fetchall() == [{"name": "Journal of Tests (JoT)"}]
    assert db.execute("SELECT md5, item FROM pending_files").fetchall() == [
        {"md5": "md5-of-a", "item": "AAAA2222"}]
    (run_dir,) = (tmp_path / "runs" / "seed").glob("2*")
    assert jobs.read_manifest(run_dir)["status"] == "ok"
    assert "3 of 3 items created" in (run_dir / "report.md").read_text()


def test_seed_uses_the_full_record_where_there_is_one(db, index, records, member):
    seed(db, index, member, records)
    full = db.execute("SELECT * FROM items WHERE key = 'AAAA2222'").fetchone()
    assert full["data"]["volume"] == "12" and full["data"]["pages"] == "1-20"
    assert full["date"] == "2025-03-04" and full["citation_key"] == "lovelaceNotes2025"
    assert full["creators"][0] == {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}
    from_index = db.execute("SELECT * FROM items WHERE key = 'BBBB3333'").fetchone()
    assert from_index["creators"] == [
        {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
        {"creatorType": "author", "name": "Plato"}]
    assert (from_index["doi"], from_index["year"]) == ("10.1000/b", 2025)
    assert from_index["other_ids"] == ["arxiv:2501.00001"]  # its recorded preprint still finds it
    assert from_index["data"]["publicationTitle"] == "Journal of Tests"
    keys = db.execute("SELECT name_key FROM item_creators WHERE item = 'BBBB3333'"
                      " ORDER BY name_key").fetchall()
    assert [k["name_key"] for k in keys] == ["lovelace", "plato"]


def test_seed_copies_vectors_unchanged(db, index, records, member):
    seed(db, index, member, records)

    def stored(sql):
        text = db.execute(sql).fetchone()["v"]
        return struct.pack(f"<{DIM}e", *json.loads(text))

    assert stored("SELECT embedding::text AS v FROM items WHERE key = 'BBBB3333'") == blob(2)
    assert stored("SELECT embedding::text AS v FROM passages WHERE idx = 2") == blob(12)
    rows = db.execute("SELECT idx, part, pages, text, artefact, embedding IS NULL AS empty"
                      " FROM passages ORDER BY idx").fetchall()
    assert [tuple(r.values()) for r in rows] == [
        (0, "body", "1", "First passage.", False, False),
        (1, "body", "1-2", "1.1 0.1 2.2 0.2", True, True),
        (2, "footnote", "2", "A footnote.", False, False)]
    assert db.execute("SELECT value FROM meta WHERE name = 'embedding_revision'").fetchone() == {
        "value": "rev-1"}


def test_seed_twice_changes_nothing(db, index, records, member):
    seed(db, index, member, records)
    before = db.execute("SELECT key, added_at, citation_key FROM items ORDER BY key").fetchall()
    counts = seed(db, index, member, records)
    assert (counts["created"], counts["present"]) == (0, 3)
    assert db.execute("SELECT key, added_at, citation_key FROM items ORDER BY key").fetchall() == before
    assert (table_count(db, "passages"), table_count(db, "pending_files")) == (3, 1)


def test_seed_imports_no_tags_or_notes(db, index, records, member):
    seed(db, index, member, records)
    everything = json.dumps(db.execute("SELECT data, creators, other_ids FROM items").fetchall())
    assert "own-work" not in everything and "COLL1" not in everything
    assert "tags" not in everything and "collections" not in everything


def test_an_item_whose_identifier_is_held_is_skipped_and_listed(db, index, records, member):
    from scriptorium_server.items import admit

    held, _ = admit(db, {"itemType": "journalArticle", "title": "On Forms", "DOI": "10.1000/B"},
                    "venue")
    counts = seed(db, index, member, records)
    assert counts["created"] == 2
    assert counts["collisions"] == [f"BBBB3333: its identifier belongs to {held}"]


def test_an_index_of_another_model_or_revision_is_refused(db, index, member):
    db.execute("INSERT INTO meta (name, value) VALUES ('embedding_revision', 'rev-0')")
    with pytest.raises(ValueError, match="rev-1.*rev-0"):
        seed(db, index, member)
    con = sqlite3.connect(index)
    con.execute("UPDATE meta SET value = 'other/model' WHERE key = 'embedding_model'")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="other/model"):
        seed(db, index, member)
    assert table_count(db, "items") == 0


def test_seed_needs_an_active_member(db, index):
    with pytest.raises(ValueError, match="no active member named 'nobody'"):
        seed(db, index, "nobody")


class FakeZotero:
    """Zotero's local API: pages of top-level entries."""

    def __init__(self, entries):
        self.entries, self.calls = entries, []

    def get(self, url, params, timeout):
        self.calls.append((url, params["start"]))
        page = self.entries[params["start"]:params["start"] + 2]  # small pages
        return type("R", (), {"json": lambda self: page, "raise_for_status": lambda self: None})()


def test_export_writes_full_records_without_tags_collections_or_notes(tmp_path):
    entries = [
        {"data": {"key": "AAAA2222", "itemType": "journalArticle", "title": "Notes", "volume": "12",
                  "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
                  "tags": [{"tag": "own-work"}], "collections": ["COLL1"], "relations": {"x": "y"},
                  "extra": "arXiv: 2501.00001", "issue": "", "version": 7}},
        {"data": {"key": "NOTE0001", "itemType": "note", "note": "private thoughts"}},
        {"data": {"key": "ATTA0001", "itemType": "attachment", "parentItem": "AAAA2222"}},
        {"data": {"key": "BBBB3333", "itemType": "preprint", "title": "Forms"}},
    ]
    zotero = FakeZotero(entries)
    out = tmp_path / "records.jsonl"
    assert export_records(out, "http://zotero.test/api", zotero) == 2
    written = [json.loads(line) for line in out.read_text().splitlines()]
    assert written[0] == {"key": "AAAA2222", "itemType": "journalArticle", "title": "Notes",
                          "volume": "12", "extra": "arXiv: 2501.00001", "creators": [
                              {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}]}
    assert written[1]["key"] == "BBBB3333"
    assert "private thoughts" not in out.read_text() and "own-work" not in out.read_text()
    assert [start for _, start in zotero.calls] == [0, 2, 4]
    assert zotero.calls[0][0] == "http://zotero.test/api/users/0/items/top"
