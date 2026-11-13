"""Backup and restore: a restore gives the same rows and files; old backups
are removed; a restore refuses a target that is not empty."""

import json
import uuid
from datetime import datetime, timedelta

import psycopg
import pytest
from typer.testing import CliRunner

from scriptorium_server import admin, backup, db as database, members, settings
from scriptorium_server.items import admit

RECORD = {"itemType": "journalArticle", "title": "Echo chambers in online news: a review",
          "DOI": "10.1000/abc", "date": "2025",
          "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}]}
SHA = "ab" + "0" * 62
NOW = datetime(2026, 11, 13, 3, 30)


@pytest.fixture
def filled(db, tmp_path):
    """A database with an item, a passage with a vector, a member, a held PDF
    and a usage-log row; a data folder with that PDF and a parsed text."""
    key, _ = admit(db, RECORD, "venue")
    members.add(db, "ada")
    vector = "[" + ",".join(["0.5"] * 256) + "]"
    db.execute("INSERT INTO passages (item, idx, part, text, embedding) VALUES (%s, 0, 'body', %s, %s)",
               (key, "the quick model of the network", vector))
    db.execute("INSERT INTO files (sha256, item, bytes, source_md5) VALUES (%s, %s, 3, 'm')",
               (SHA, key))
    db.execute("INSERT INTO usage_log (member, request, inputs, status) VALUES (1, 'lookup', '{}', 200)")
    db.commit()
    data = tmp_path / "data"
    (data / "pdfs" / "ab").mkdir(parents=True)
    (data / "pdfs" / "ab" / f"{SHA}.pdf").write_bytes(b"pdf")
    (data / "pdfs" / ".1-x.part").write_bytes(b"half written")
    (data / "tei").mkdir()
    (data / "tei" / f"{key}.tei.xml.gz").write_bytes(b"tei")
    (data / "tei" / "parsed.jsonl").write_text("{}\n")
    return key, data


@pytest.fixture
def second_schema(test_database):
    """A connection to another empty schema: the target of a restore."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(test_database, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    conn = database.connect(test_database, schema)
    yield conn
    conn.close()
    with psycopg.connect(test_database, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


def counts(conn):
    conn.rollback()
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
            for t in ("items", "passages", "members", "files", "usage_log", "meta", "jobs")}


def test_backup_then_restore_gives_the_same_counts(db, filled, second_schema, tmp_path):
    key, data = filled
    folder = tmp_path / "backups"
    result = backup.backup(db, data, folder, 14, NOW)
    assert result["tables"]["items"]["rows"] == 1 and result["copied"] == {"pdfs": 1, "tei": 2}
    assert "tsv" not in result["tables"]["items"]["columns"]  # computed by the database

    target = tmp_path / "restored"
    restored = backup.restore(second_schema, backup.find(folder, "latest"), target)
    assert counts(second_schema) == counts(db)
    assert restored["pdfs"] == 1 and restored["tei"] == 2
    assert (target / "pdfs" / "ab" / f"{SHA}.pdf").read_bytes() == b"pdf"
    assert (target / "tei" / f"{key}.tei.xml.gz").read_bytes() == b"tei"
    assert not list((target / "pdfs").glob(".*"))  # the half-written file was not backed up

    same = "SELECT title, creators, embedding::text AS e FROM items"
    assert second_schema.execute(same).fetchall() == db.execute(same).fetchall()
    passage = "SELECT text, embedding::text AS e FROM passages"
    assert second_schema.execute(passage).fetchall() == db.execute(passage).fetchall()
    # The computed column is there again, and numbering goes on after the restored rows.
    assert second_schema.execute("SELECT tsv @@ to_tsquery('simple', 'echo') AS hit FROM items"
                                 ).fetchone()["hit"]
    assert members.add(second_schema, "grace") and second_schema.execute(
        "SELECT id FROM members WHERE name = 'grace'").fetchone()["id"] == 2


def test_second_backup_copies_only_new_files(db, filled, tmp_path):
    _, data = filled
    folder = tmp_path / "backups"
    backup.backup(db, data, folder, 14, NOW)
    (data / "pdfs" / "cd").mkdir()
    (data / "pdfs" / "cd" / "new.pdf").write_bytes(b"new")
    (data / "tei" / "parsed.jsonl").write_text("{}\n{}\n")  # the record grows
    result = backup.backup(db, data, folder, 14, NOW + timedelta(days=1))
    assert result["copied"] == {"pdfs": 1, "tei": 1} and result["files"] == {"pdfs": 2, "tei": 2}
    assert (folder / "files" / "tei" / "parsed.jsonl").read_text() == "{}\n{}\n"
    assert len(backup.runs(folder)) == 2


def test_old_backups_are_removed(db, filled, tmp_path):
    _, data = filled
    folder = tmp_path / "backups"
    for day in (0, 1, 5):
        backup.backup(db, data, folder, 3, NOW + timedelta(days=day))
    names = [r.name for r in backup.runs(folder)]
    assert names == ["2026-11-18-033000"]  # day 0 and 1 were older than 3 days at day 5
    unfinished = folder / "runs" / "2026-11-10-033000"
    unfinished.mkdir()
    assert backup.prune(folder, 3, NOW + timedelta(days=5)) == ["2026-11-10-033000"]
    # The newest finished backup stays however old it is.
    assert backup.prune(folder, 3, NOW + timedelta(days=400)) == []
    assert [r.name for r in backup.runs(folder)] == names


def test_restore_refuses_a_target_that_is_not_empty(db, filled, second_schema, tmp_path):
    _, data = filled
    folder = tmp_path / "backups"
    with pytest.raises(backup.BackupError, match="no finished backup"):
        backup.find(folder, "latest")
    result = backup.backup(db, data, folder, 14, NOW)
    run = backup.find(folder, "2026-11-13-033000")
    assert str(run) == result["run"]
    with pytest.raises(backup.BackupError, match="database is not empty"):
        backup.restore(db, run, tmp_path / "x")
    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore(second_schema, run, data)
    second_schema.rollback()
    assert second_schema.execute(
        "SELECT count(*) AS n FROM pg_class WHERE relnamespace = current_schema()::regnamespace"
    ).fetchone()["n"] == 0  # a refused restore leaves the database empty


def test_restore_refuses_a_backup_it_cannot_read(db, filled, second_schema, tmp_path):
    _, data = filled
    folder = tmp_path / "backups"
    result = backup.backup(db, data, folder, 14, NOW)
    run = backup.find(folder, "latest")
    with pytest.raises(backup.BackupError, match="no finished backup named"):
        backup.find(folder, "2020-01-01-000000")
    # A backup made with a migration this server does not know.
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["migrations"].append("9999_future.sql")
    (run / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(backup.BackupError, match="other migrations"):
        backup.restore(second_schema, run, tmp_path / "x")
    # A PDF the database refers to is missing from the backup.
    manifest["migrations"].pop()
    (run / "manifest.json").write_text(json.dumps(manifest))
    (folder / "files" / "pdfs" / "ab" / f"{SHA}.pdf").unlink()
    with pytest.raises(backup.BackupError, match="lacks the PDF"):
        backup.restore(second_schema, run, tmp_path / "y")
    assert result["files"]["pdfs"] == 1


def test_backup_and_restore_commands(db, filled, empty_schema, second_schema, tmp_path, monkeypatch):
    _, data = filled
    path = tmp_path / "settings.toml"
    monkeypatch.setenv("SCRIPTORIUM_SETTINGS", str(path))
    settings.set_value("data.folder", str(data), path)
    settings.set_value("backup.folder", str(tmp_path / "backups"), path)
    monkeypatch.setattr(admin, "connect", lambda: database.connect(*empty_schema))
    made = CliRunner().invoke(admin.app, ["backup"])
    assert made.exit_code == 0 and "1 PDFs (1 new)" in made.output
    refused = CliRunner().invoke(admin.app, ["restore", "latest"])
    assert refused.exit_code == 1 and "not empty" in refused.output
    assert CliRunner().invoke(admin.app, ["restore", "nope"]).exit_code == 1
