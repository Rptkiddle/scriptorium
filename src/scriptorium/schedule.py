"""The nightly surveillance schedule: the LaunchAgent that starts it and
the dashboard's view of it. The only macOS-specific code in the
surveillance arm.
"""

import datetime
import os
import subprocess
from pathlib import Path

NIGHTLY_LABEL = "com.rupertkiddle.scriptorium-surveillance"
NIGHTLY_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{NIGHTLY_LABEL}.plist"
# The Scriptorium.app launcher stub (see app/): running through the app
# gives the nightly job the app's scoped Desktop-folder permission.
_APP_BIN = (
    Path.home() / "Applications" / "Scriptorium.app" / "Contents" / "MacOS" / "Scriptorium"
)


def reconcile_nightly_schedule(enabled: bool, hour: int) -> None:
    """Make the nightly LaunchAgent match settings: firing at `hour` when
    surveillance.enabled, unloaded and disabled otherwise.

    Called by the daemon whenever surveillance settings are saved — the
    master switch in the settings GUI is the only control. launchd runs a
    missed calendar firing once on wake, so a sleeping laptop catches up.
    """
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{NIGHTLY_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{_APP_BIN}</string>
    <string>surveil</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>{int(hour)}</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
"""
    changed = not (NIGHTLY_PLIST.exists() and NIGHTLY_PLIST.read_text() == plist)
    if changed:
        NIGHTLY_PLIST.write_text(plist)
    domain = f"gui/{os.getuid()}"

    def run(*args):
        return subprocess.run(["launchctl", *args], capture_output=True, text=True,
                              check=False)

    if enabled:
        run("enable", f"{domain}/{NIGHTLY_LABEL}")
        if changed:
            run("bootout", f"{domain}/{NIGHTLY_LABEL}")
        # bootstrap also fails when the job is already loaded (the normal
        # case), so success is judged by whether the job is loaded after.
        boot = run("bootstrap", domain, str(NIGHTLY_PLIST))
        if run("print", f"{domain}/{NIGHTLY_LABEL}").returncode != 0:
            raise RuntimeError(
                f"nightly job not loaded: {(boot.stderr or boot.stdout).strip()}")
    else:
        run("bootout", f"{domain}/{NIGHTLY_LABEL}")
        run("disable", f"{domain}/{NIGHTLY_LABEL}")


def schedule_status(
    last_date: str | None, now: datetime.datetime, nightly_hour: int,
    catchup_days: int,
) -> dict:
    """Dashboard view of the nightly schedule. next_run is the next
    scheduled start; pending counts scheduled nights since the last
    successful run that have not yet succeeded (the next run catches them
    up); missed means the gap already exceeds the catch-up window, so
    some days will not be covered without a manual --from harvest."""
    today = now.date()
    next_day = today if now.hour < nightly_hour else today + datetime.timedelta(days=1)
    next_run = datetime.datetime.combine(next_day, datetime.time(nightly_hour))
    if not last_date:
        return {"next_run": next_run.isoformat(), "pending": 0, "missed": False}
    last = datetime.date.fromisoformat(last_date)
    latest_due = today if now.hour >= nightly_hour else today - datetime.timedelta(days=1)
    pending = max(0, (latest_due - last).days)
    return {
        "next_run": next_run.isoformat(),
        "pending": pending,
        "missed": (next_day - last).days > catchup_days,
    }
