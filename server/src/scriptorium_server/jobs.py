"""Run folders: the one layout every job's outputs share.

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
from pathlib import Path

FINAL_STATUSES = ("ok", "partial", "failed")
MANIFEST = "run.json"
REPO = Path(__file__).resolve().parents[3]


def git_state() -> dict:
    """The code version in use: commit and uncommitted files (empty when
    the code does not run from a git checkout)."""
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
