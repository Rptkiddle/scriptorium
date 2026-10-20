"""The nightly surveillance launcher: what launchd starts at the nightly
hour, through Scriptorium.app (app/dispatch.sh surveil), so the run holds
the app's Desktop-folder permission.

In order: refuse to run when the repository has uncommitted changes (the
night's run must use committed code; recorded as a failed run), wait for
the network, make sure Zotero and the Scriptorium Bridge answer, run one
surveillance cycle, check that the repository did not change during the
run, and post a notification with the outcome. Output goes to
~/Library/Logs/scriptorium/surveillance.log; everything the run produces
stays in its run folder.
"""

import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

from scriptorium.config import load_config
from scriptorium.runs import REPO, finish, git_state, new_run, update_manifest, work_dir
from scriptorium.surveillance_cycle import run_cycle
from scriptorium.zotero_bridge import ZoteroBridge


def _log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def _wait(check, attempts: int, pause: float) -> bool:
    for i in range(attempts):
        try:
            if check():
                return True
        except Exception:  # noqa: BLE001, S110 — not up yet; the caller logs the timeout
            pass
        if i < attempts - 1:
            time.sleep(pause)
    return False


def _network_up() -> bool:
    try:
        urllib.request.urlopen("https://api.anthropic.com", timeout=10)
    except urllib.error.HTTPError:
        pass  # any HTTP answer means the network is up
    return True


def _bridge_up() -> bool:
    ZoteroBridge(timeout=5).ping()
    return True


def _notify(message: str) -> None:
    text = message.replace("\\", "").replace('"', "")[:200]
    subprocess.run(["osascript", "-e",
                    f'display notification "{text}" with title "scriptorium surveillance"'],
                   capture_output=True, check=False)


def _ensure_zotero() -> None:
    if _wait(_bridge_up, 1, 0):
        return
    _log("opening Zotero")
    subprocess.run(["open", "-g", "-j", "-a", "Zotero"], capture_output=True, check=False)
    if not _wait(_bridge_up, 36, 5):
        _log("WARNING: Zotero Bridge not answering after 3 minutes; additions will be retried")


def main() -> int:
    config = load_config()
    _log(f"=== surveillance run {datetime.now():%Y-%m-%d} ===")
    before = git_state()
    run_dir = new_run(config, "surveillance")
    _log(f"run folder: {run_dir}")
    try:
        return _run(config, run_dir, before)
    except Exception as exc:  # noqa: BLE001 — a launchd failure must reach the run record
        summary = f"launcher failed: {type(exc).__name__}: {exc}"[:300]
        finish(run_dir, "failed", summary)
        _log(summary)
        _notify(f"FAILED — {summary}")
        return 1


def _run(config, run_dir, before: dict) -> int:
    update_manifest(run_dir, started_by="launchd")
    if before["dirty"]:
        summary = ("refused: the repository has uncommitted changes ("
                   + ", ".join(before["uncommitted"])[:200] + ")")
        update_manifest(run_dir, provenance=before)
        finish(run_dir, "failed", summary)
        _log(summary)
        _notify(summary)
        return 1

    if not _wait(_network_up, 20, 30):
        _log("WARNING: network still down after 10 minutes")
    _ensure_zotero()

    status, summary = run_cycle(config, run_dir)

    after = git_state()
    if (after["commit"], after["uncommitted"]) != (before["commit"], before["uncommitted"]):
        diff = subprocess.run(["git", "-C", str(REPO), "diff", before["commit"]],
                              capture_output=True, text=True, check=False).stdout
        (work_dir(run_dir) / "repo_changed.diff").write_text(diff)
        update_manifest(run_dir, repo_changed={"before": before, "after": after})
        status, summary = "failed", "the repository changed during the run (see work/repo_changed.diff)"
        finish(run_dir, status, summary)

    _log(f"status: {status} — {summary}")
    _notify(summary if status != "failed" else f"FAILED — {summary}")
    _log(f"=== surveillance {status} {datetime.now():%H:%M:%S} ===")
    return 0 if status in ("ok", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
