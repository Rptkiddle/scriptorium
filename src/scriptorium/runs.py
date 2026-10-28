"""Run folders: the one layout every arm's outputs share.

Each arm (surveillance, interrogation, discovery, retrieval) keeps its
outputs in the data directory under <data>/<arm>/:

    INDEX.md                         one line per run, rebuilt from the manifests
    state/                           arm-level persistent state, where an arm has any
    runs/<YYYY-MM-DD-HHMMSS>[-<slug>]/
        run.json                     manifest: arm, run, status, started, finished,
                                     session, plus the arm's own sections
        report.md                    the readable result
        <deliverables>               the arm's other results, at the run root
        work/                        every intermediate file: prompts, verdicts, pools

The folder name is the run's ID, in local time; run.json carries the exact
timestamps and the code version the run started with (commit and
uncommitted files). A run's status is "running" until its last step
finishes it as "ok", "partial" (finished, with something left to do) or
"failed". A later change of status (a retried add completing a partial
run, say) updates status and summary but never rewrites "finished": every
finish is appended to the run's "history", with who made it.

Command line, for the skills and the nightly launcher:

    python -m scriptorium.runs new <arm> [--slug S] [--session ID]
        creates a run folder and prints its path
    python -m scriptorium.runs finish <rundir> --status ok|partial|failed [--summary S]
        closes a run and rebuilds its arm's INDEX.md
"""

import argparse
import datetime
import json
import os
import subprocess
from pathlib import Path

from scriptorium.config import Config, load_config

ARMS = ("surveillance", "interrogation", "discovery", "retrieval", "ingestion")
FINAL_STATUSES = ("ok", "partial", "failed")
MANIFEST = "run.json"
REPO = Path(__file__).resolve().parents[2]


def git_state() -> dict:
    """The code version in use: commit and uncommitted files."""
    def git(*args):
        return subprocess.run(["git", "-C", str(REPO), *args],
                              capture_output=True, text=True, check=False).stdout.strip()
    status = git("status", "--porcelain")
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(status),
            "uncommitted": status.splitlines()}


def _now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


def arm_dir(config: Config, arm: str) -> Path:
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {', '.join(ARMS)}")
    return config.index_dir / arm


def new_run(
    config: Config, arm: str, slug: str | None = None, session: str | None = None,
) -> Path:
    """Create runs/<YYYY-MM-DD-HHMMSS>[-<slug>]/ with its work/ folder and an
    initial manifest. The Claude session is recorded when known, so a run
    can be traced back to the conversation that produced it."""
    now = _now()
    name = now.strftime("%Y-%m-%d-%H%M%S") + (f"-{slug}" if slug else "")
    run_dir = arm_dir(config, arm) / "runs" / name
    try:
        (run_dir / "work").mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise FileExistsError(
            f"{run_dir} already exists; start the run in another second") from None
    write_manifest(run_dir, {
        "arm": arm,
        "run": name,
        "status": "running",
        "started": now.isoformat(timespec="seconds"),
        "finished": None,
        "session": session or os.environ.get("CLAUDE_CODE_SESSION_ID"),
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


def finish(run_dir: Path, status: str, summary: str | None = None,
           by: str = "self", **sections) -> dict:
    """Close a run, or record a later change of its status, then rebuild
    the arm's INDEX.md. "finished" is set by the first call only; every
    call appends {at, status, summary, by} to the run's history. `by`
    names who made the change: "self" for the run's own steps, otherwise
    the run or session that did (e.g. a later run retrying its add)."""
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
    rebuild_index(run_dir.parent.parent)
    return manifest


def rebuild_index(arm_path: Path) -> Path:
    """Write <arm>/INDEX.md from the run manifests, newest first."""
    runs = arm_path / "runs"
    lines = [
        f"# {arm_path.name} runs",
        "",
        ("One line per run, newest first: run ID (the folder in runs/), status,"
        " summary. Each run's run.json holds the detail."),
        "",
    ]
    for d in sorted(runs.iterdir() if runs.is_dir() else [], reverse=True):
        if not d.is_dir():
            continue
        m = read_manifest(d)
        if not m:
            lines.append(f"- `{d.name}` — no run.json")
            continue
        summary = f" — {m['summary']}" if m.get("summary") else ""
        lines.append(f"- `{d.name}` — {m.get('status', '?')}{summary}")
    index = arm_path / "INDEX.md"
    index.write_text("\n".join(lines) + "\n")
    return index


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m scriptorium.runs")
    sub = ap.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create a run folder and print its path")
    new.add_argument("arm", choices=ARMS)
    new.add_argument("--slug")
    new.add_argument("--session")
    fin = sub.add_parser("finish", help="close a run and rebuild its INDEX.md")
    fin.add_argument("rundir")
    fin.add_argument("--status", required=True, choices=FINAL_STATUSES)
    fin.add_argument("--summary")
    fin.add_argument("--by", default="self")
    args = ap.parse_args()

    if args.command == "new":
        print(new_run(load_config(), args.arm, args.slug, args.session))
    else:
        finish(Path(args.rundir).expanduser(), args.status, args.summary, by=args.by)


if __name__ == "__main__":
    main()
