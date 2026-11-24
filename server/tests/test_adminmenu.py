"""The admin menu through the API: for admins only, every change previewed
first, made on confirm and written to the admin log."""

import datetime
import json

import pytest
from fastapi.testclient import TestClient

from scriptorium_server import db as database, jobs, members, settings
from scriptorium_server.api import create_app
from scriptorium_server.embedding import FakeEmbedder
from scriptorium_server.items import admit


@pytest.fixture
def world(db, empty_schema, tmp_path):
    """A small collection, an admin (ada) and a plain member (grace), each
    with a client; the server reads its settings file anew at each request."""
    path = tmp_path / "settings.toml"
    settings.set_value("data.folder", str(tmp_path / "data"), path)
    settings.set_value("backup.folder", str(tmp_path / "backups"), path)
    venue = db.execute("INSERT INTO venues (name, adapter, source_id, publisher, category,"
                       " subscribed) VALUES ('Journal of Tests', 'openalex', 'S1', 'A Press',"
                       " 'Journals', true) RETURNING id").fetchone()["id"]
    key, _ = admit(db, {"itemType": "journalArticle", "title": "Echo chambers", "DOI": "10.1/a",
                        "date": "2021", "creators": []}, "venue")
    db.execute("UPDATE items SET venue_id = %s WHERE key = %s", (venue, key))
    keys = {name: members.add(db, name) for name in ("ada", "grace")}
    members.set_admin(db, "ada")
    db.commit()
    app = create_app(lambda: database.connect(*empty_schema), FakeEmbedder(),
                     settings.load(path), current=lambda: settings.load(path))

    def client(name):
        return TestClient(app, headers={"Authorization": f"Bearer {keys[name]}",
                                        "X-Scriptorium-Client": "0.4.0"})
    return type("World", (), {"admin": client("ada"), "member": client("grace"), "db": db,
                              "path": path, "tmp": tmp_path, "keys": keys})


def changes(world):
    world.db.rollback()
    return world.db.execute("SELECT m.name, a.action, a.target, a.before, a.after FROM admin_log a"
                            " JOIN members m ON m.id = a.member ORDER BY a.id").fetchall()


def test_the_menu_is_for_admins_only(world):
    assert world.admin.get("/v1/health").json()["admin"] is True
    assert world.member.get("/v1/health").json()["admin"] is False
    assert world.member.get("/v1/admin/overview").status_code == 403
    for name in ("settings", "members", "venues", "runs", "jobs"):
        reply = world.member.post(f"/v1/admin/{name}", json={})
        assert reply.status_code == 403 and "admins" in reply.json()["error"]
        assert world.admin.post(f"/v1/admin/{name}", json={}).status_code == 200
    assert TestClient(world.admin.app).get("/v1/admin/overview").status_code == 401
    world.db.rollback()
    logged = world.db.execute("SELECT request, status FROM usage_log WHERE request LIKE 'admin/%%'"
                              " AND status = 403", ()).fetchall()
    assert len(logged) == 6  # refused requests are in the usage log like any other


def test_overview_gives_the_state_on_one_page(world):
    settings.set_value("limits.per_request", 250, world.path)
    reply = world.admin.get("/v1/admin/overview").json()
    assert reply["collection"]["items"] == 1 and reply["collection"]["subscribed_venues"] == 1
    assert reply["collection"]["items_waiting_for_a_vector"] == 1
    assert reply["members"] == {"active": 2, "admins": 1, "active_last_7_days": 0}
    assert reply["settings_changed_from_default"]["limits.per_request"] == "250"
    assert reply["schedule"]["last_harvest"] is None and reply["schedule"]["queued_or_running"] == []
    assert reply["schedule"]["next_harvest_and_backup"][11:] == "03:00"
    assert set(reply["tools"]) == {"admin_settings", "admin_members", "admin_venues",
                                   "admin_runs", "admin_jobs"}


def test_a_setting_is_previewed_then_changed_on_confirm_and_takes_effect(world):
    every = world.admin.post("/v1/admin/settings", json={}).json()["settings"]
    assert len(every) == len(settings.SETTINGS)
    one = world.admin.post("/v1/admin/settings", json={"name": "limits.per_request"}).json()
    assert one["setting"]["value"] == "1000" and one["setting"]["takes_effect"] == "at once"
    ask = {"name": "limits.per_request", "value": "2"}
    preview = world.admin.post("/v1/admin/settings", json=ask).json()
    assert preview["changed"] is False and preview["preview"]["from"] == "1000"
    assert preview["preview"]["to"] == "2" and "confirm=true" in preview["next"]
    assert settings.load(world.path)["limits.per_request"] == 1000 and changes(world) == []
    done = world.admin.post("/v1/admin/settings", json={**ask, "confirm": True}).json()
    assert done["changed"] is True and settings.load(world.path)["limits.per_request"] == 2
    assert changes(world) == [{"name": "ada", "action": "settings.set",
                               "target": "limits.per_request", "before": "1000", "after": "2"}]
    refused = world.member.post("/v1/items", json={"count": 3})  # the new limit is in force
    assert refused.status_code == 400 and "limit per request" in refused.json()["error"]
    again = world.admin.post("/v1/admin/settings", json={**ask, "confirm": True}).json()
    assert again["changed"] is False and "already" in again["note"]


def test_secrets_host_settings_and_bad_values_are_refused(world):
    settings.set_value("sources.openalex_api_key", "SECRET-KEY", world.path)
    shown = {d["name"]: d for d in world.admin.post("/v1/admin/settings", json={}).json()["settings"]}
    assert shown["sources.openalex_api_key"]["value"] == "set"
    assert "SECRET-KEY" not in json.dumps(shown)
    assert shown["data.folder"]["changed_here"] is False
    for name, value in (("sources.openalex_api_key", "x"), ("data.folder", "/elsewhere"),
                        ("database.address", "postgresql://x")):
        reply = world.admin.post("/v1/admin/settings", json={"name": name, "value": value,
                                                             "confirm": True})
        assert reply.status_code == 400 and "on the host only" in reply.json()["error"]
    bad = world.admin.post("/v1/admin/settings", json={"name": "surveillance.hour", "value": "24"})
    assert bad.status_code == 400 and "0 to 23" in bad.json()["error"]
    assert world.admin.post("/v1/admin/settings", json={"name": "no.such"}).status_code == 400
    assert changes(world) == []


def test_members_are_listed_with_activity_added_and_revoked(world):
    world.member.post("/v1/items", json={"count": 0})
    listed = {m["name"]: m for m in world.admin.post("/v1/admin/members", json={}).json()["members"]}
    assert listed["ada"]["admin"] is True and listed["grace"]["admin"] is False
    assert listed["grace"]["requests"] == 1 and listed["grace"]["last_request"]
    assert listed["grace"]["status"] == "active"
    add = {"action": "add", "name": "hedy"}
    assert world.admin.post("/v1/admin/members", json=add).json()["changed"] is False
    assert members.listing(world.db)[-1]["name"] == "grace"
    reply = world.admin.post("/v1/admin/members", json={**add, "confirm": True}).json()
    key = reply["api_key"]
    world.db.rollback()
    assert members.authenticate(world.db, key)["name"] == "hedy"
    stored = world.db.execute("SELECT reply::text AS reply FROM usage_log").fetchall() \
        + world.db.execute("SELECT after::text AS reply FROM admin_log").fetchall()
    assert all(key not in (row["reply"] or "") for row in stored)  # shown once, logged nowhere
    assert world.admin.post("/v1/admin/members", json={**add, "confirm": True}).status_code == 400
    revoke = {"action": "revoke", "name": "grace"}
    assert world.admin.post("/v1/admin/members", json=revoke).json()["changed"] is False
    assert world.member.get("/v1/health").status_code == 200
    assert world.admin.post("/v1/admin/members", json={**revoke, "confirm": True}).json() == {
        "changed": True, "revoked": "grace"}
    assert world.member.get("/v1/health").status_code == 401
    own = world.admin.post("/v1/admin/members", json={"action": "revoke", "name": "ada",
                                                      "confirm": True})
    assert own.status_code == 400 and "yourself" in own.json()["error"]
    assert [(c["action"], c["target"]) for c in changes(world)] == [
        ("members.add", "hedy"), ("members.revoke", "grace")]


def test_venues_are_listed_with_their_items_added_and_removed(world):
    listed = world.admin.post("/v1/admin/venues", json={"contains": "tests"}).json()
    assert listed["total"] == 1 and listed["venues"][0]["items"] == 1
    assert world.admin.post("/v1/admin/venues", json={"contains": "zzz"}).json()["total"] == 0
    add = {"action": "add", "name": "Political Communication",
           "source_id": "https://openalex.org/S2", "publisher": "Routledge"}
    preview = world.admin.post("/v1/admin/venues", json=add).json()
    assert preview["changed"] is False and "90 days" in preview["preview"]["what_happens"]
    assert world.admin.post("/v1/admin/venues", json={}).json()["total"] == 1
    done = world.admin.post("/v1/admin/venues", json={**add, "confirm": True}).json()
    assert done["subscribed"]["source_id"] == "S2" and done["subscribed"]["category"] == "Journals"
    for bad in ({"action": "add", "name": "No Source"},
                {"action": "add", "name": "Same Source", "source_id": "S2"},
                {"action": "remove", "name": "Unknown"}, {"action": "add"}):
        assert world.admin.post("/v1/admin/venues", json={**bad, "confirm": True}).status_code == 400
    remove = {"action": "remove", "name": "journal of tests"}
    assert "1 items stay" in world.admin.post("/v1/admin/venues", json=remove).json()[
        "preview"]["what_happens"]
    assert world.admin.post("/v1/admin/venues", json={**remove, "confirm": True}).json() == {
        "changed": True, "unsubscribed": "journal of tests", "items_kept": 1}
    names = [v["name"] for v in world.admin.post("/v1/admin/venues", json={}).json()["venues"]]
    assert names == ["Political Communication"]
    world.db.rollback()
    assert world.db.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 1
    assert [c["action"] for c in changes(world)] == ["venues.add", "venues.remove"]


def test_runs_are_listed_and_started_as_jobs(world):
    folder = jobs.new_run(world.tmp / "data" / "runs", "surveillance")
    jobs.finish_run(folder, "ok", "3 new, 0 already held")
    listed = world.admin.post("/v1/admin/runs", json={}).json()
    assert [(r["kind"], r["status"], r["summary"]) for r in listed["runs"]] == [
        ("surveillance", "ok", "3 new, 0 already held")]
    assert listed["queued_or_running"] == []
    start = {"action": "start", "kind": "surveillance", "dry_run": True}
    preview = world.admin.post("/v1/admin/runs", json=start).json()
    assert preview["changed"] is False and "nothing is written" in preview["preview"]["what_happens"]
    world.db.rollback()
    assert world.db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
    done = world.admin.post("/v1/admin/runs", json={**start, "confirm": True}).json()
    world.db.rollback()
    job = world.db.execute("SELECT * FROM jobs WHERE id = %s", (done["job"],)).fetchone()
    assert job["kind"] == "surveillance" and job["status"] == "waiting" and job["member"] is None
    assert job["payload"] == {"requested_by": "ada", "dry_run": True}
    assert world.admin.post("/v1/admin/runs", json={**start, "confirm": True}).status_code == 400
    assert world.admin.post("/v1/admin/runs", json={"action": "start", "kind": "backup",
                                                    "confirm": True}).json()["queued"] == "backup"
    waiting = world.admin.post("/v1/admin/runs", json={}).json()["queued_or_running"]
    assert [(r["kind"], r["status"], r["started_by"]) for r in waiting] == [
        ("surveillance", "waiting", "an admin"), ("backup", "waiting", "an admin")]
    for bad in ({"action": "start"}, {"action": "start", "kind": "load"},
                {"action": "start", "kind": "backup", "dry_run": True}):
        assert world.admin.post("/v1/admin/runs", json=bad).status_code == 400
    assert [c["action"] for c in changes(world)] == ["runs.start", "runs.start"]


def test_jobs_are_listed_shown_and_a_failed_one_is_retried(world):
    grace = world.db.execute("SELECT id FROM members WHERE name = 'grace'").fetchone()["id"]
    ids = [jobs.enqueue(world.db, "fulltext", {"identifier": f"10.1/{n}"}, grace) for n in range(3)]
    world.db.execute("UPDATE jobs SET status = 'failed', error = %s WHERE id = %s",
                     ("GrobidUnavailable: no answer\nmore", ids[0]))
    world.db.execute("UPDATE jobs SET status = 'failed', error = %s WHERE id = %s",
                     ("Rejected: not kept: no body text", ids[1]))
    world.db.commit()
    listed = world.admin.post("/v1/admin/jobs", json={"status": "failed"}).json()
    assert {(c["status"], c["n"]) for c in listed["counts"]} == {("failed", 2), ("waiting", 1)}
    assert {r["reason"] for r in listed["failures_by_reason"]} == {
        "GrobidUnavailable: no answer", "Rejected: not kept: no body text"}
    assert [j["id"] for j in listed["jobs"]] == [ids[1], ids[0]] and listed["jobs"][0]["member"] == "grace"
    shown = world.admin.post("/v1/admin/jobs", json={"action": "show", "job_id": ids[0]}).json()
    assert shown["member"] == "grace" and shown["error"].endswith("more")
    retry = {"action": "retry", "job_id": ids[0]}
    assert world.admin.post("/v1/admin/jobs", json=retry).json()["changed"] is False
    assert world.admin.post("/v1/admin/jobs", json={**retry, "confirm": True}).json() == {
        "changed": True, "retried": ids[0]}
    world.db.rollback()
    assert world.db.execute("SELECT status FROM jobs WHERE id = %s", (ids[0],)).fetchone()[
        "status"] == "waiting"
    for job_id in (ids[1], ids[2], 99999):  # rejected for good, not failed, unknown
        reply = world.admin.post("/v1/admin/jobs", json={"action": "retry", "job_id": job_id,
                                                         "confirm": True})
        assert reply.status_code == 400
    assert changes(world)[0]["action"] == "jobs.retry"


def test_overview_shows_the_last_backup_and_recent_changes(world):
    from scriptorium_server import backup as backups

    result = backups.backup(world.db, world.tmp / "data", world.tmp / "backups", 14,
                            now=datetime.datetime(2026, 11, 23, 3, 5))
    world.db.commit()
    world.admin.post("/v1/admin/settings", json={"name": "backup.days_kept", "value": "30",
                                                 "confirm": True})
    reply = world.admin.get("/v1/admin/overview").json()
    assert reply["schedule"]["last_backup"]["run"] == "2026-11-23-030500"
    assert f"{len(result['tables'])} tables" in reply["schedule"]["last_backup"]["summary"]
    assert reply["recent_admin_changes"][0]["target"] == "backup.days_kept"
    assert reply["recent_admin_changes"][0]["by"] == "ada"


def test_a_schedule_change_that_starts_a_run_says_so(world):
    now = datetime.datetime(2026, 11, 30, 10, 0)  # a Monday, seven hours after 03:00
    from scriptorium_server import adminmenu

    def ask(name, value, moment=now):
        ada = {"id": world.db.execute("SELECT id FROM members WHERE name = 'ada'").fetchone()["id"],
               "name": "ada"}
        return adminmenu.settings_request(world.db, ada, settings.load(world.path), name, value,
                                          now=moment)
    jobs.enqueue(world.db, "surveillance", {"scheduled": "2026-11-30T03:00"})  # the week's run
    earlier = ask("surveillance.hour", "8")["preview"]
    assert "2026-11-30T08:00" in earlier["starts_a_run"] and "within a minute" in earlier["starts_a_run"]
    assert "starts_a_run" not in ask("surveillance.hour", "11")["preview"]  # still to come
    assert "starts_a_run" not in ask("surveillance.day", "thursday")["preview"]
