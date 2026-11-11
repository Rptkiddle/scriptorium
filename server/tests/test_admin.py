"""The admin command: members and keys, venues, jobs, settings."""

import pytest
from typer.testing import CliRunner

import scriptorium_server
from scriptorium_server import admin, db as database, jobs, members


@pytest.fixture
def run(db, empty_schema, tmp_path, monkeypatch):
    """Run the admin command against the test's own schema and settings file."""
    monkeypatch.setattr(admin, "connect", lambda: database.connect(*empty_schema))
    monkeypatch.setenv("SCRIPTORIUM_SETTINGS", str(tmp_path / "settings.toml"))

    def invoke(*args):
        result = CliRunner().invoke(admin.app, list(args))
        db.rollback()  # see what the command committed
        return result

    return invoke


def test_version_command_prints_the_version(run):
    result = run("version")
    assert result.exit_code == 0 and result.output.strip() == scriptorium_server.__version__


def test_migrate_reports_an_up_to_date_schema(run):
    assert "up to date" in run("migrate").output


def test_member_key_is_shown_once_and_not_stored(run, db):
    result = run("members", "add", "ada")
    key = result.output.strip().splitlines()[-1]
    assert result.exit_code == 0 and key.startswith("scr_") and len(key) > 40
    stored = str(db.execute("SELECT * FROM members").fetchall())
    assert key not in stored and members.hash_key(key) in stored
    assert members.authenticate(db, key)["name"] == "ada"
    listing = run("members", "list").output
    assert "ada" in listing and "active" in listing and key not in listing
    again = run("members", "add", "ada")
    assert again.exit_code == 1 and "already a member named 'ada'" in again.output


def test_revoked_key_is_refused(run, db):
    key = run("members", "add", "ada").output.strip().splitlines()[-1]
    other = run("members", "add", "grace").output.strip().splitlines()[-1]
    assert run("members", "revoke", "ada").exit_code == 0
    assert members.authenticate(db, key) is None
    assert members.authenticate(db, other)["name"] == "grace"
    assert members.authenticate(db, "scr_unknown") is None and members.authenticate(db, "") is None
    assert "revoked" in run("members", "list").output
    assert run("members", "revoke", "ada").exit_code == 1  # nothing left to revoke


def test_venues_are_subscribed_listed_and_unsubscribed(run, db):
    added = run("venues", "add", "Journal of Tests", "--adapter", "crossref",
                "--issn", "1234-5678", "--issn", "8765-4321", "--publisher", "Test Press",
                "--category", "Journals")
    assert added.exit_code == 0
    run("venues", "add", "Mega Journal", "--adapter", "openalex", "--needs-filter")
    listing = run("venues", "list").output
    assert "Journal of Tests\tcrossref\t1234-5678, 8765-4321\tnot yet harvested" in listing
    assert "Mega Journal\topenalex\t-\tnot yet harvested\tneeds filter" in listing
    assert run("venues", "add", "X", "--adapter", "scopus").exit_code == 1
    assert run("venues", "remove", "journal of tests").exit_code == 0
    assert "Journal of Tests" not in run("venues", "list").output
    assert db.execute("SELECT count(*) AS n FROM venues").fetchone()["n"] == 2  # the row stays
    assert run("venues", "remove", "Journal of Tests").exit_code == 1


def test_subscribing_to_a_venue_items_already_use_keeps_its_row(run, db):
    venue = db.execute("INSERT INTO venues (name, publisher) VALUES ('Journal of Tests', 'P')"
                       " RETURNING id").fetchone()["id"]
    db.commit()
    run("venues", "add", "journal of tests", "--adapter", "crossref", "--issn", "1234-5678")
    row = db.execute("SELECT * FROM venues").fetchone()
    assert (row["id"], row["subscribed"], row["publisher"], row["issns"]) == (
        venue, True, "P", ["1234-5678"])


def test_jobs_list_shows_failures(run, db, tmp_path):
    jobs.enqueue(db, "fulltext", {"file": "a.pdf"})
    jobs.enqueue(db, "fulltext", {"file": "b.pdf"})
    jobs.finish(db, jobs.claim(db, tmp_path), error="ValueError: wrong paper\nmore detail")
    listing = run("jobs", "list").output
    assert listing.splitlines()[0] == "1 failed, 1 waiting"
    assert "1\tfulltext\tfailed" in listing and listing.count("ValueError: wrong paper") == 1
    assert "more detail" not in listing
    only_failed = run("jobs", "list", "--status", "failed").output
    assert "waiting\t" not in only_failed.split("\n", 1)[1]
    shown = run("jobs", "show", "1").output
    assert "more detail" in shown and str(tmp_path) in shown and "a.pdf" in shown
    assert run("jobs", "show", "99").exit_code == 1


def test_settings_are_shown_and_set(run):
    shown = run("settings", "show").output
    assert "limits.per_request = 1000" in shown and "sources.s2_api_key = not set" in shown
    changed = run("settings", "set", "limits.per_request", "250")
    assert changed.exit_code == 0 and changed.output.strip() == "limits.per_request = 250"
    secret = run("settings", "set", "sources.s2_api_key", "SECRET")
    assert secret.output.strip() == "sources.s2_api_key = set" and "SECRET" not in secret.output
    bad = run("settings", "set", "limits.per_request", "lots")
    assert bad.exit_code == 1 and "limits.per_request must be" in bad.output
    assert run("settings", "set", "limits.typo", "1").exit_code == 1


def test_worker_runs_the_waiting_jobs(run, db, tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "HANDLERS", {"note": lambda conn, job: "noted"})
    run("settings", "set", "data.folder", str(tmp_path))
    jobs.enqueue(db, "note", {})
    db.commit()
    assert run("worker", "--until-idle").output.strip() == "Ran 1 job(s)."
    row = db.execute("SELECT status, run_dir FROM jobs").fetchone()
    assert row["status"] == "done" and row["run_dir"].startswith(str(tmp_path / "runs" / "note"))
