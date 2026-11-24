"""The weekly schedule: the worker queues the harvest and the backup once,
at the moment the settings name, and runs them as jobs."""

import datetime

from scriptorium_server import admin, jobs, schedule, settings, surveillance

MONDAY_3 = datetime.datetime(2026, 11, 30, 3, 0)


def current(tmp_path, **values):
    path = tmp_path / "settings.toml"
    for name, value in values.items():
        settings.set_value(name.replace("__", "."), value, path)
    return settings.load(path)


def test_the_moment_is_the_day_and_hour_of_the_settings(tmp_path):
    now = current(tmp_path)  # monday 03:00 by default
    assert schedule.moment(now, MONDAY_3) == MONDAY_3
    assert schedule.moment(now, MONDAY_3 - datetime.timedelta(minutes=1)) == \
        MONDAY_3 - datetime.timedelta(days=7)
    assert schedule.moment(now, datetime.datetime(2026, 12, 3, 12, 0)) == MONDAY_3
    assert schedule.moment(now, MONDAY_3, after=True) == MONDAY_3 + datetime.timedelta(days=7)
    friday = current(tmp_path, surveillance__day="friday", surveillance__hour=22)
    assert schedule.moment(friday, MONDAY_3, after=True) == datetime.datetime(2026, 12, 4, 22, 0)
    off = current(tmp_path, surveillance__day="off")
    assert schedule.moment(off, MONDAY_3) is None


def test_the_harvest_and_the_backup_are_queued_once_at_their_moment(db, tmp_path):
    now = current(tmp_path)
    assert schedule.due(db, now, MONDAY_3 - datetime.timedelta(minutes=1)) == []  # a week old
    ids = schedule.due(db, now, MONDAY_3 + datetime.timedelta(seconds=40))
    rows = db.execute("SELECT kind, status, payload, member FROM jobs ORDER BY id").fetchall()
    assert len(ids) == 2 and [r["kind"] for r in rows] == ["surveillance", "backup"]
    assert rows[0]["payload"] == {"scheduled": "2026-11-30T03:00"} and rows[0]["member"] is None
    assert schedule.due(db, now, MONDAY_3 + datetime.timedelta(minutes=5)) == []  # not twice
    db.execute("UPDATE jobs SET status = 'done'")
    assert schedule.due(db, now, MONDAY_3 + datetime.timedelta(hours=2)) == []  # nor once done
    week_later = schedule.due(db, now, MONDAY_3 + datetime.timedelta(days=7, hours=1))
    assert len(week_later) == 2  # a worker that started an hour late still runs them


def test_a_moment_missed_by_more_than_the_grace_waits_for_next_week(db, tmp_path):
    now = current(tmp_path)
    assert schedule.due(db, now, MONDAY_3 + datetime.timedelta(hours=13)) == []
    assert schedule.due(db, current(tmp_path, surveillance__day="off"), MONDAY_3) == []


def test_the_worker_runs_a_harvest_and_a_backup_as_jobs(db, empty_schema, tmp_path, monkeypatch):
    now = current(tmp_path, data__folder=str(tmp_path / "data"),
                  backup__folder=str(tmp_path / "backups"), sources__openalex_api_key="KEY",
                  surveillance__window_days=30, surveillance__max_cost=2, surveillance__parallel=3)
    asked = {}

    def surveil(conn, venues, run_dir, **options):
        asked.update(options, venues=len(venues), run_dir=run_dir)
        return {"since": "2026-10-31", "venues": 1, "requests": 1, "cost_usd": 0.0, "seconds": 1,
                "new": 4, "fetched": 9, "dry_run": options["dry_run"]}
    monkeypatch.setattr(surveillance, "surveil", surveil)
    db.execute("INSERT INTO venues (name, adapter, source_id, subscribed) VALUES"
               " ('A', 'openalex', 'S1', true), ('B', 'openalex', 'S2', false)")
    harvest = jobs.enqueue(db, "surveillance", {"scheduled": "2026-11-30T03:00"})
    backup = jobs.enqueue(db, "backup", {"scheduled": "2026-11-30T03:00"})
    db.commit()
    ran = jobs.run_worker(db, tmp_path / "data" / "runs", admin.job_handlers(now), until_idle=True)
    assert ran == 2
    assert asked["venues"] == 1 and asked["days"] == 30 and asked["api_key"] == "KEY"
    assert asked["parallel"] == 3 and asked["max_cost"] == 2.0 and asked["dry_run"] is False
    rows = {r["id"]: r for r in db.execute("SELECT * FROM jobs").fetchall()}
    assert rows[harvest]["status"] == "done" and "4 new" in rows[harvest]["summary"]
    assert rows[harvest]["run_dir"] == str(asked["run_dir"])
    assert jobs.read_manifest(asked["run_dir"])["status"] == "ok"
    assert rows[backup]["status"] == "done" and rows[backup]["summary"].startswith("Backup ")
    assert len(list((tmp_path / "backups" / "runs").iterdir())) == 1


def test_a_harvest_that_stops_fails_its_job_without_showing_the_key(db, tmp_path, monkeypatch):
    now = current(tmp_path, data__folder=str(tmp_path / "data"), sources__openalex_api_key="KEY")

    def surveil(conn, venues, run_dir, **options):
        raise RuntimeError("https://api.openalex.org/works?api_key=KEY")
    monkeypatch.setattr(surveillance, "surveil", surveil)
    job = jobs.enqueue(db, "surveillance", {"dry_run": True})
    db.commit()
    jobs.run_worker(db, tmp_path / "data" / "runs", admin.job_handlers(now), until_idle=True)
    row = db.execute("SELECT status, error FROM jobs WHERE id = %s", (job,)).fetchone()
    assert row["status"] == "failed" and "KEY" not in row["error"]
    assert "RuntimeError" in row["error"]


def test_a_harvest_without_a_key_fails_with_the_reason(db, tmp_path, monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    now = current(tmp_path, data__folder=str(tmp_path / "data"))
    job = jobs.enqueue(db, "surveillance", {})
    db.commit()
    jobs.run_worker(db, tmp_path / "data" / "runs", admin.job_handlers(now), until_idle=True)
    row = db.execute("SELECT status, error FROM jobs WHERE id = %s", (job,)).fetchone()
    assert row["status"] == "failed" and "NoKey" in row["error"]


def test_the_worker_looks_at_the_schedule_before_the_queue(db, tmp_path):
    calls = []

    def before(conn):
        calls.append(1)
        if len(calls) == 1:
            jobs.enqueue(conn, "note", {})
            conn.commit()
        else:
            raise RuntimeError("a fault here does not stop the worker")
    done = []
    ran = jobs.run_worker(db, tmp_path, {"note": lambda conn, job: done.append(job.id) or "ok"},
                          until_idle=True, before=before)
    assert ran == 1 and len(done) == 1 and len(calls) == 2
