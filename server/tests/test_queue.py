"""The job queue and the worker: each job runs once, an interrupted job
runs again after a restart, a failure is kept with its error and run folder."""

import threading

from scriptorium_server import db as database
from scriptorium_server.jobs import (
    claim,
    enqueue,
    finish,
    read_manifest,
    requeue_unfinished,
    run_worker,
)


def job_row(db, job_id):
    return db.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


def test_claim_gives_each_job_once(db, empty_schema, tmp_path):
    ids = [enqueue(db, "fulltext", {"n": n}) for n in range(20)]
    db.commit()
    claimed: list[int] = []

    def worker():
        with database.connect(*empty_schema) as conn:
            while (job := claim(conn, tmp_path)) is not None:
                claimed.append(job.id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(claimed) == ids  # every job, and none twice
    assert claim(db, tmp_path) is None


def test_jobs_run_oldest_first_with_their_payload_and_run_folder(db, tmp_path):
    member = db.execute("INSERT INTO members (name, key_hash) VALUES ('m', 'h')"
                        " RETURNING id").fetchone()["id"]
    first = enqueue(db, "fulltext", {"file": "a.pdf"}, member)
    enqueue(db, "intake", {})
    job = claim(db, tmp_path)
    assert (job.id, job.kind, job.payload, job.member) == (first, "fulltext", {"file": "a.pdf"}, member)
    assert job.run_dir.parent == tmp_path / "fulltext" and job.run_dir.name.endswith(f"-job-{first}")
    assert (job.run_dir / "work").is_dir()
    row = job_row(db, first)
    assert row["status"] == "running" and row["run_dir"] == str(job.run_dir) and row["started_at"]
    finish(db, job, summary="70 passages")
    row = job_row(db, first)
    assert row["status"] == "done" and row["finished_at"] and row["error"] is None
    manifest = read_manifest(job.run_dir)
    assert (manifest["status"], manifest["summary"]) == ("ok", "70 passages")


def test_unfinished_job_runs_again_after_restart(db, tmp_path):
    """Review focus 3: the worker stops mid-job."""
    job_id = enqueue(db, "fulltext", {"file": "a.pdf"})
    interrupted = claim(db, tmp_path)  # the worker took it, then stopped
    assert job_row(db, job_id)["status"] == "running"

    runs: list[int] = []
    assert run_worker(db, tmp_path, {"fulltext": lambda conn, job: runs.append(job.id)},
                      until_idle=True) == 1
    assert runs == [job_id]  # once
    row = job_row(db, job_id)
    assert row["status"] == "done" and row["run_dir"] != str(interrupted.run_dir)
    assert read_manifest(tmp_path / "fulltext" / row["run_dir"].rsplit("/", 1)[1])["status"] == "ok"
    old = read_manifest(interrupted.run_dir)
    assert (old["status"], old["summary"]) == ("failed", "the worker stopped during this run")
    assert requeue_unfinished(db) == 0  # a finished job is not run again
    assert run_worker(db, tmp_path, {}, until_idle=True) == 0


def test_failed_job_keeps_its_error_and_run_folder(db, tmp_path):
    def handler(conn, job):
        (job.run_dir / "work" / "notes.txt").write_text("got this far")
        conn.execute("INSERT INTO meta (name, value) VALUES ('half', 'done')")
        raise ValueError("the PDF is not the paper it claims to be")

    failed = enqueue(db, "fulltext", {})
    later = enqueue(db, "fulltext", {})
    ran: list[int] = []
    handlers = {"fulltext": lambda conn, job: handler(conn, job) if job.id == failed
                else ran.append(job.id)}
    assert run_worker(db, tmp_path, handlers, until_idle=True) == 2
    row = job_row(db, failed)
    assert row["status"] == "failed"
    assert row["error"] == "ValueError: the PDF is not the paper it claims to be"
    run_dir = tmp_path / "fulltext" / row["run_dir"].rsplit("/", 1)[1]
    assert (run_dir / "work" / "notes.txt").read_text() == "got this far"
    assert read_manifest(run_dir)["status"] == "failed"
    assert db.execute("SELECT count(*) AS n FROM meta").fetchone()["n"] == 0  # its writes undone
    assert ran == [later] and job_row(db, later)["status"] == "done"  # the worker went on


def test_a_job_of_an_unknown_kind_fails_with_a_reason(db, tmp_path):
    job_id = enqueue(db, "mystery", {})
    assert run_worker(db, tmp_path, {}, until_idle=True) == 1
    assert job_row(db, job_id)["error"] == "no handler for jobs of kind 'mystery'"


def test_the_worker_stops_when_told(db, tmp_path):
    for _ in range(3):
        enqueue(db, "intake", {})
    seen: list[int] = []
    run_worker(db, tmp_path, {"intake": lambda conn, job: seen.append(job.id)},
               stop=lambda: len(seen) >= 2)
    assert len(seen) == 2
    assert db.execute("SELECT count(*) AS n FROM jobs WHERE status = 'waiting'").fetchone()["n"] == 1
