"""The database: migrations, the vector extension, and the schema's rules."""

import psycopg
import pytest

from scriptorium_server import db as database

ITEM = ("INSERT INTO items (key, type, title, title_norm, doi, arxiv_id, provenance_kind)"
        " VALUES (%s, 'journalArticle', 'A Title', 'a title', %s, %s, %s)")
TABLES = {"items", "item_creators", "passages", "venues", "members", "files", "jobs",
          "usage_log", "meta", "pending_files",
          "schema_migrations"}


def vector(*head: float) -> str:
    return "[" + ",".join(str(x) for x in (*head, *[0.0] * (256 - len(head)))) + "]"


def test_migrate_creates_every_table(db):
    rows = db.execute("SELECT table_name FROM information_schema.tables"
                      " WHERE table_schema = current_schema()").fetchall()
    assert {r["table_name"] for r in rows} == TABLES


def test_migrate_is_repeatable(empty_schema):
    with database.connect(*empty_schema) as conn:
        assert database.migrate(conn) == [name for name, _ in database.migration_files()]
        assert database.migrate(conn) == []
    with database.connect(*empty_schema) as conn:  # a later start of the server
        assert database.migrate(conn) == []
        count = conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"]
        assert count == len(database.migration_files())


def test_a_failing_migration_leaves_nothing_behind(empty_schema, monkeypatch):
    good = database.migration_files()
    monkeypatch.setattr(database, "migration_files",
                        lambda: [*good, ("9999_broken.sql", "CREATE TABLE x (); SELECT nope")])
    with database.connect(*empty_schema) as conn:
        with pytest.raises(psycopg.Error):
            database.migrate(conn)
        conn.rollback()
        tables = conn.execute("SELECT count(*) AS n FROM information_schema.tables"
                              " WHERE table_schema = current_schema()").fetchone()["n"]
        assert tables == 0


def test_vector_extension_present(db):
    assert db.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    db.execute(ITEM, ("K1", None, None, "donation"))
    db.execute(ITEM, ("K2", None, None, "donation"))
    db.execute("UPDATE items SET embedding = %s WHERE key = 'K1'", (vector(1, 0),))
    db.execute("UPDATE items SET embedding = %s WHERE key = 'K2'", (vector(0, 1),))
    nearest = db.execute("SELECT key FROM items ORDER BY embedding <=> %s::halfvec LIMIT 1",
                         (vector(0.1, 0.9),)).fetchone()
    assert nearest["key"] == "K2"
    with pytest.raises(psycopg.Error):  # the dimension is fixed
        db.execute("UPDATE items SET embedding = '[1,2,3]' WHERE key = 'K1'")


def test_both_vector_columns_have_a_cosine_hnsw_index(db):
    rows = db.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema()"
                      " AND indexname IN ('items_embedding', 'passages_embedding')").fetchall()
    assert len(rows) == 2
    assert all("hnsw" in r["indexdef"] and "halfvec_cosine_ops" in r["indexdef"] for r in rows)


def test_doi_is_unique_ignoring_case(db):
    db.execute(ITEM, ("K1", "10.1000/ABC", None, "venue"))
    db.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(ITEM, ("K2", "10.1000/abc", None, "venue"))
    db.rollback()
    db.execute(ITEM, ("K3", None, None, "venue"))  # items without a DOI do not collide
    db.execute(ITEM, ("K4", None, None, "venue"))


def test_arxiv_id_is_unique_ignoring_case(db):
    db.execute(ITEM, ("K1", None, "hep-th/9901001", "venue"))
    db.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(ITEM, ("K2", None, "HEP-TH/9901001", "venue"))


def test_provenance_kind_is_one_of_three(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(ITEM, ("K1", None, None, "import"))


def test_keyword_columns_follow_the_text(db):
    db.execute(ITEM, ("K1", None, None, "donation"))
    db.execute("UPDATE items SET abstract = 'Echo chambers revisited' WHERE key = 'K1'")
    db.execute("INSERT INTO passages (item, idx, part, text) VALUES ('K1', 0, 'body', 'filter bubbles')")
    hit = db.execute("SELECT key FROM items WHERE tsv @@ phraseto_tsquery('simple', 'echo chambers')")
    assert hit.fetchone()["key"] == "K1"
    hit = db.execute("SELECT idx FROM passages WHERE tsv @@ plainto_tsquery('simple', 'bubbles')")
    assert hit.fetchone()["idx"] == 0


def test_deleting_an_item_removes_its_passages_creators_and_files(db):
    db.execute(ITEM, ("K1", None, None, "donation"))
    db.execute("INSERT INTO passages (item, idx, part, text) VALUES ('K1', 0, 'body', 't')")
    db.execute("INSERT INTO item_creators (item, name_key, position) VALUES ('K1', 'kiddle', 0)")
    db.execute("INSERT INTO files (sha256, item, bytes) VALUES ('abc', 'K1', 10)")
    db.execute("DELETE FROM items WHERE key = 'K1'")
    for table in ("passages", "item_creators", "files"):
        assert db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0


def test_one_pdf_can_belong_to_two_items_after_migrating_a_filled_database(empty_schema):
    """0002 is applied to a collection that already holds items and awaited
    files (as the live server does when it starts); afterwards one PDF can
    be recorded for two items, both as awaited and as stored."""
    (first, first_sql), *later = database.migration_files()
    with database.connect(*empty_schema) as conn:
        conn.execute(first_sql)
        conn.execute("CREATE TABLE schema_migrations (name text PRIMARY KEY,"
                     " applied_at timestamptz NOT NULL DEFAULT now())")
        conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (first,))
        conn.execute(ITEM, ("K1", None, None, "donation"))
        conn.execute(ITEM, ("K2", None, None, "donation"))
        conn.execute("INSERT INTO pending_files (md5, item) VALUES ('m1', 'K1')")
        conn.commit()
        assert database.migrate(conn) == [name for name, _ in later]
        conn.execute("INSERT INTO pending_files (md5, item) VALUES ('m1', 'K2')")
        conn.execute("INSERT INTO files (sha256, item, bytes, source_md5) VALUES ('s1', 'K1', 9, 'm1')")
        conn.execute("INSERT INTO files (sha256, item, bytes, source_md5) VALUES ('s1', 'K2', 9, 'm1')")
        rows = conn.execute("SELECT md5, item FROM pending_files ORDER BY item").fetchall()
        assert [(r["md5"], r["item"]) for r in rows] == [("m1", "K1"), ("m1", "K2")]
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO pending_files (md5, item) VALUES ('m1', 'K1')")
