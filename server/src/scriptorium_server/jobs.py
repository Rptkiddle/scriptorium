"""Jobs: the queue the worker takes its work from, and the run folder each
job writes to.

THE QUEUE is the table `jobs`. enqueue() adds a waiting job; the worker
claims the oldest waiting job, runs the handler registered for its kind,
and records it as done or failed with the error. A job that was running
when the worker stopped is put back to waiting when the worker starts
again, so a handler must be safe to run a second time. One worker runs at
a time; claim() itself hands each job to one claimant only.

RUN FOLDERS are the one layout every job's outputs share.

Each kind of job keeps its outputs under <runs folder>/<kind>/:

    INDEX.md                         one line per run, rebuilt from the manifests
    <YYYY-MM-DD-HHMMSS>[-<slug>]/
        run.json                     manifest: kind, run, status, started, finished,
                                     plus the job's own sections
        report.md                    the readable result
        <deliverables>               the job's other results, at the run root
        work/                        every intermediate file

The folder name is the run's ID, in local time; run.json carries the exact
timestamps and the code version the run started with (commit and
uncommitted files). A run's status is "running" until its last step
finishes it as "ok", "partial" (finished, with something left to do) or
"failed". A later change of status updates status and summary but never
rewrites "finished": every finish is appended to the run's "history", with
who made it.
"""

import datetime
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

FINAL_STATUSES = ("ok", "partial", "failed")
MANIFEST = "run.json"
REPO = Path(__file__).resolve().parents[3]


def git_state() -> dict:
    """The code version in use: commit and uncommitted files. In a built
    image there is no git checkout; the commit it was built from is in
    SCRIPTORIUM_COMMIT."""
    if commit := os.environ.get("SCRIPTORIUM_COMMIT"):
        return {"commit": commit, "dirty": False, "uncommitted": []}
    def git(*args):
        return subprocess.run(["git", "-C", str(REPO), *args],
                              capture_output=True, text=True, check=False).stdout.strip()
    status = git("status", "--porcelain")
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(status),
            "uncommitted": status.splitlines()}


def _now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


def new_run(runs_dir: Path, kind: str, slug: str | None = None) -> Path:
    """Create <runs_dir>/<kind>/<YYYY-MM-DD-HHMMSS>[-<slug>]/ with its work/
    folder and an initial manifest."""
    if not kind or "/" in kind or kind.startswith("."):
        raise ValueError(f"not a job kind: {kind!r}")
    now = _now()
    name = now.strftime("%Y-%m-%d-%H%M%S") + (f"-{slug}" if slug else "")
    run_dir = Path(runs_dir) / kind / name
    try:
        (run_dir / "work").mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise FileExistsError(
            f"{run_dir} already exists; start the run in another second") from None
    write_manifest(run_dir, {
        "kind": kind,
        "run": name,
        "status": "running",
        "started": now.isoformat(timespec="seconds"),
        "finished": None,
        "provenance": git_state(),
    })
    return run_dir


def fresh_run(runs_dir: Path, kind: str, slug: str | None = None) -> Path:
    """new_run, with "run2", "run3", ... added to the slug when a run of
    that name was started within the same second."""
    attempt = 1
    while True:
        extra = f"run{attempt}" if attempt > 1 else ""
        try:
            return new_run(runs_dir, kind, "-".join(p for p in (slug, extra) if p) or None)
        except FileExistsError:
            attempt += 1


def work_dir(run_dir: Path) -> Path:
    """The run's folder for intermediate files (created if missing)."""
    path = run_dir / "work"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_manifest(run_dir: Path) -> dict:
    path = run_dir / MANIFEST
    return json.loads(path.read_text()) if path.is_file() else {}


def write_manifest(run_dir: Path, manifest: dict) -> None:
    tmp = run_dir / f"{MANIFEST}.tmp"
    tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    os.replace(tmp, run_dir / MANIFEST)


def update_manifest(run_dir: Path, **sections) -> dict:
    """Merge top-level sections into the manifest and return it."""
    manifest = read_manifest(run_dir)
    manifest.update(sections)
    write_manifest(run_dir, manifest)
    return manifest


def finish_run(run_dir: Path, status: str, summary: str | None = None,
               by: str = "self", **sections) -> dict:
    """Close a run, or record a later change of its status, then rebuild
    the kind's INDEX.md. "finished" is set by the first call only; every
    call appends {at, status, summary, by} to the run's history. `by`
    names who made the change: "self" for the run's own steps, otherwise
    the run that did (e.g. a later run retrying a step)."""
    if status not in FINAL_STATUSES:
        raise ValueError(f"status must be one of {', '.join(FINAL_STATUSES)}")
    manifest = read_manifest(run_dir)
    now = _now().isoformat(timespec="seconds")
    if summary is None:
        summary = manifest.get("summary")
    fields = {
        "status": status, "summary": summary,
        "history": manifest.get("history", []) + [
            {"at": now, "status": status, "summary": summary, "by": by}],
    }
    if not manifest.get("finished"):
        fields["finished"] = now
    manifest = update_manifest(run_dir, **fields, **sections)
    rebuild_index(run_dir.parent)
    return manifest


def rebuild_index(kind_dir: Path) -> Path:
    """Write <kind>/INDEX.md from the run manifests, newest first."""
    lines = [
        f"# {kind_dir.name} runs",
        "",
        ("One line per run, newest first: run ID (the folder), status,"
         " summary. Each run's run.json holds the detail."),
        "",
    ]
    for d in sorted(kind_dir.iterdir() if kind_dir.is_dir() else [], reverse=True):
        if not d.is_dir():
            continue
        m = read_manifest(d)
        if not m:
            lines.append(f"- `{d.name}` — no run.json")
            continue
        summary = f" — {m['summary']}" if m.get("summary") else ""
        lines.append(f"- `{d.name}` — {m.get('status', '?')}{summary}")
    index = kind_dir / "INDEX.md"
    index.write_text("\n".join(lines) + "\n")
    return index


# --- the queue -----------------------------------------------------------------


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    member: int | None
    run_dir: Path


Handler = Callable[[psycopg.Connection, Job], str | None]  # returns a one-line summary


def enqueue(conn: psycopg.Connection, kind: str, payload: dict,
            member: int | None = None) -> int:
    """Add a waiting job; returns its id."""
    return conn.execute("INSERT INTO jobs (kind, payload, member) VALUES (%s, %s, %s)"
                        " RETURNING id", (kind, Jsonb(payload), member)).fetchone()["id"]


def claim(conn: psycopg.Connection, runs_dir: Path) -> Job | None:
    """Take the oldest waiting job: mark it running, give it a run folder,
    commit. None when nothing waits. Two claimants never get the same job."""
    row = conn.execute(
        "UPDATE jobs SET status = 'running', started_at = now(), finished_at = NULL,"
        " error = NULL WHERE id = (SELECT id FROM jobs WHERE status = 'waiting'"
        " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id, kind, payload, member"
    ).fetchone()
    if row is None:
        conn.commit()
        return None
    # a job run again within the same second gets a folder of its own
    run_dir = fresh_run(runs_dir, row["kind"], f"job-{row['id']}")
    conn.execute("UPDATE jobs SET run_dir = %s WHERE id = %s", (str(run_dir), row["id"]))
    conn.commit()
    return Job(row["id"], row["kind"], row["payload"], row["member"], run_dir)


def finish(conn: psycopg.Connection, job: Job, error: str | None = None,
           summary: str | None = None) -> None:
    """Record a claimed job as done, or as failed with its error, and close
    its run folder the same way."""
    conn.execute("UPDATE jobs SET status = %s, finished_at = now(), error = %s, summary = %s"
                 " WHERE id = %s", ("failed" if error else "done", error,
                                    None if error else summary, job.id))
    conn.commit()
    finish_run(job.run_dir, "failed" if error else "ok", error or summary)


def retry(conn: psycopg.Connection, job_id: int) -> bool:
    """Put a failed job back to waiting; False when there is no failed job
    of that id. Its old run folder and error stay in the record of that run."""
    row = conn.execute("UPDATE jobs SET status = 'waiting', started_at = NULL, finished_at = NULL,"
                       " error = NULL WHERE id = %s AND status = 'failed' RETURNING id",
                       (job_id,)).fetchone()
    conn.commit()
    return row is not None


def requeue_unfinished(conn: psycopg.Connection) -> int:
    """Put every job left running by a worker that stopped back to waiting,
    and close its old run folder as failed; returns how many. Called once
    when the worker starts."""
    rows = conn.execute("UPDATE jobs SET status = 'waiting', started_at = NULL"
                        " WHERE status = 'running' RETURNING run_dir").fetchall()
    conn.commit()
    for row in rows:
        if row["run_dir"] and Path(row["run_dir"]).is_dir():
            finish_run(Path(row["run_dir"]), "failed", "the worker stopped during this run")
    return len(rows)


def run_worker(conn: psycopg.Connection, runs_dir: Path, handlers: dict[str, Handler], *,
               poll_seconds: float = 5.0, until_idle: bool = False,
               stop: Callable[[], bool] = lambda: False,
               idle: Callable[[psycopg.Connection], object] | None = None) -> int:
    """Run waiting jobs one at a time, oldest first, until `stop()` is true
    (or, with until_idle, until nothing waits); returns how many ran. A
    handler that raises fails its job with the error; the worker goes on.
    `idle` is called whenever the queue is empty (the worker embeds what
    waits for a vector there); an error in it is printed, not raised."""
    requeue_unfinished(conn)
    ran = 0
    while not stop():
        job = claim(conn, runs_dir)
        if job is None:
            if idle is not None:
                try:
                    idle(conn)
                except Exception as exc:  # noqa: BLE001 — the worker must keep running
                    conn.rollback()
                    print(f"while idle: {type(exc).__name__}: {exc}", flush=True)
            if until_idle:
                return ran
            time.sleep(poll_seconds)
            continue
        ran += 1
        handler = handlers.get(job.kind)
        if handler is None:
            finish(conn, job, error=f"no handler for jobs of kind {job.kind!r}")
            continue
        try:
            summary = handler(conn, job)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — any failure belongs to the job, not the worker
            conn.rollback()
            finish(conn, job, error=f"{type(exc).__name__}: {exc}")
        else:
            finish(conn, job, summary=summary)
    return ran
