"""scriptorium menubar companion.

A thin view/controller over the daemon: polls /health for status, checks
Zotero's local API and the Scriptorium Bridge directly, and drives the
daemon's LaunchAgent via
launchctl. It holds no hub state of its own — the daemon can run without it,
and it can run while the daemon is stopped.

Icon colour is the status summary:
  green  daemon up, Zotero up, index matches Zotero's library version
  amber  daemon and Zotero up, but the index differs from Zotero (the library
         changed since the last sync) or the last sync failed
Failed items (PDFs without readable body text, e.g. scans) are counted in the
menu but leave the icon green: most stay failed for good, retried only when
their PDF changes.
  red    daemon unreachable; Zotero closed (which disables the staleness
         check, so the index cannot be trusted as current); or the
         Scriptorium Bridge plugin not loaded (every library write fails)

Poll intervals are read at startup; restart the menubar after changing them.

Run with `uv run scriptorium-menubar` (needs the `menubar` extra installed).
"""

import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import rumps
from fastmcp.exceptions import ToolError

from scriptorium.config import load_config
from scriptorium.hub_client import call_tool
from scriptorium.surveillance import state_path
from scriptorium.zotero_bridge import ZoteroBridge

_BASE = f"http://localhost:{load_config().port}"

HEALTH_URL = _BASE + "/health"
ZOTERO_VERSIONS_URL = "http://localhost:23119/api/users/0/items?limit=1&format=versions"
LABEL = "io.github.rptkiddle.scriptorium"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SURVEIL_STATE = state_path(load_config())
LOG_DIR = Path.home() / "Library" / "Logs" / "scriptorium"
ASSETS = Path(__file__).parent / "assets"
ICONS = {c: str(ASSETS / f"menubar-{c}.png") for c in ("green", "amber", "red")}

SETTINGS_URL = _BASE + "/settings.json"


def general_settings() -> dict:
    """The general settings section from the daemon (defaults if unreachable)."""
    try:
        with urllib.request.urlopen(SETTINGS_URL, timeout=1.0) as r:
            return json.loads(r.read())["general"]
    except Exception:
        return {"menubar_health_interval": 5, "menubar_staleness_interval": 60}


def daemon_health() -> dict | None:
    """Return /health JSON, or None if the daemon is unreachable."""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1.0) as r:
            return json.loads(r.read())
    except Exception:
        return None


def zotero_library_version() -> int | None:
    """Current Zotero library version from the local API, or None if closed."""
    try:
        with urllib.request.urlopen(ZOTERO_VERSIONS_URL, timeout=1.0) as r:
            return int(r.headers["Last-Modified-Version"])
    except Exception:
        return None


def bridge_status() -> dict | None:
    """The Scriptorium Bridge's ping answer, or None if it cannot be reached
    (Zotero closed, or the plugin not loaded)."""
    try:
        return ZoteroBridge(timeout=1.0).ping()
    except Exception:
        return None


def summarize(
    health: dict | None, zotero_version: int | None, bridge: dict | None
) -> tuple[str, str, str]:
    """Icon colour, status line and index line for one poll."""
    if health is None:
        return "red", "Daemon stopped", "Sync: unavailable"
    papers, chunks = health["papers"], health["chunks"]
    sync = health.get("sync")
    if sync:
        status = f"Syncing — {sync['done']}/{sync['total']} ({sync['current']})"
        return "amber", status, f"Index: {papers} papers, {chunks} chunks"

    failed = health.get("failed", 0)
    items = health.get("catalogue_items", 0)
    pct = round(100 * papers / items) if items else 0
    status = (
        f"Running — {compact(items)} items, "
        f"{compact(papers)} pdfs, {compact(chunks)} chunks ({pct}% coverage)"
    )
    watermark = health.get("library_version") or 0
    sync_error = health.get("last_sync_error")
    # Red states first: a closed Zotero also makes syncs fail, and "closed"
    # is the message that says what to fix.
    if zotero_version is None:
        return "red", status, "Sync: unknown (Zotero closed)"
    if bridge is None:
        return "red", status, "Zotero Bridge not loaded — writes will fail"
    if sync_error:
        return "amber", status, f"Sync: failing — {sync_error['error'][:80]}"
    failures = f", {failed} failed item(s)" if failed else ""
    if zotero_version != watermark:
        return "amber", status, (
            f"Sync: behind Zotero (v{watermark} vs v{zotero_version})" + failures)
    return "green", status, "Sync: up to date" + failures


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def compact(n: int) -> str:
    """5200 -> '5.2k', 27944 -> '28k', 615 -> '615'."""
    if n < 1_000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{round(n / 1000)}k"


def surveillance_status() -> str:
    """One-line summary of the last surveillance run, from its state file."""
    import datetime

    try:
        last = json.loads(SURVEIL_STATE.read_text()).get("last_run")
    except Exception:
        last = None
    if not last:
        return "Surveillance: never run"
    when = last["date"]
    if when == datetime.date.today().isoformat():
        when = "today"
    return f"Surveillance: {when} — {last['candidates']} → {last['accepted']} accepted"


class ScriptoriumMenubar(rumps.App):
    def __init__(self) -> None:
        super().__init__("scriptorium", icon=ICONS["red"], quit_button=None)
        self.status_item = rumps.MenuItem("Checking…")
        self.index_item = rumps.MenuItem("Index: …")
        self.surveil_item = rumps.MenuItem("Surveillance: …")
        self.sync_item = rumps.MenuItem("Sync now", callback=self.sync)
        self.toggle_item = rumps.MenuItem("Start daemon", callback=self.toggle)
        self.menu = [
            self.status_item,
            self.index_item,
            self.surveil_item,
            None,
            self.sync_item,
            rumps.MenuItem("Open dashboard", callback=self.open_dashboard),
            rumps.MenuItem("View surveillance", callback=self.view_surveillance),
            self.toggle_item,
            rumps.MenuItem("Settings…", callback=self.open_settings),
            rumps.MenuItem("Open logs", callback=self.open_logs),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        self.daemon_up = False
        self.syncing = False
        self.sync_active = False
        self.zotero_version: int | None = None
        self.bridge: dict | None = None
        self._tick = 0
        self._color = "red"
        general = general_settings()
        interval = general["menubar_health_interval"]
        self.staleness_every = max(1, general["menubar_staleness_interval"] // interval)
        rumps.Timer(self.refresh, interval).start()

    # -- status ------------------------------------------------------------

    def refresh(self, _=None) -> None:
        health = daemon_health()
        self.daemon_up = health is not None
        sync = health.get("sync") if health else None
        self.sync_active = sync is not None
        # Re-check every poll while Zotero or the Bridge is down, so recovery
        # shows within seconds rather than at the next staleness interval.
        if (self._tick % self.staleness_every == 0
                or self.zotero_version is None or self.bridge is None):
            self.zotero_version = zotero_library_version()
            self.bridge = bridge_status()
        self._tick += 1

        color, status, index = summarize(health, self.zotero_version, self.bridge)
        if color != self._color:
            self._color = color
            self.icon = ICONS[color]
        self.status_item.title = status
        self.index_item.title = index
        self.surveil_item.title = surveillance_status()
        self.sync_item.title = "Pause sync" if self.sync_active else "Sync now"
        self.toggle_item.title = "Stop daemon" if self.daemon_up else "Start daemon"

    # -- actions -----------------------------------------------------------

    def toggle(self, _) -> None:
        domain = f"gui/{os.getuid()}"
        if self.daemon_up:
            r = launchctl("bootout", f"{domain}/{LABEL}")
        else:
            r = launchctl("bootstrap", domain, str(PLIST))
        if r.returncode != 0:
            rumps.alert("scriptorium", f"launchctl failed:\n{r.stderr.strip() or r.stdout.strip()}")
        self.refresh()

    def sync(self, _) -> None:
        """Start a sync, or request cancellation of the one running."""
        if not self.daemon_up:
            rumps.alert("scriptorium", "Daemon is not running — start it first.")
            return
        if self.sync_active:
            threading.Thread(
                target=lambda: call_tool("hub_sync_cancel"),
                daemon=True,
            ).start()
            return
        if self.syncing:
            return
        self.syncing = True
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self) -> None:
        try:
            data = call_tool("hub_sync")
            verb = "Sync paused" if data.get("cancelled") else "Sync complete"
            msg = (
                f"{verb} — added {data['added']}, updated {data['updated']}, "
                f"metadata {data.get('metadata_updated', 0)}, "
                f"removed {data['removed']}, unchanged {data['unchanged']}, "
                f"PDF unavailable {data.get('unavailable', 0)}, "
                f"failed {data['failed']}."
            )
        except ToolError as exc:  # e.g. a sync already running
            msg = str(exc)
        except Exception as exc:
            msg = f"Sync failed: {exc}"
        finally:
            self.syncing = False
            self.zotero_version = None  # force a fresh staleness check
        # AppKit UI must run on the main thread; this worker is not it.
        from PyObjCTools import AppHelper

        AppHelper.callAfter(rumps.alert, "scriptorium sync", msg)

    def view_surveillance(self, _) -> None:
        """Open the daemon's surveillance viewer in the browser."""
        if not self.daemon_up:
            rumps.alert("scriptorium", "Daemon is not running — start it first.")
            return
        subprocess.run(["open", _BASE + "/surveillance"], check=False)


    def open_dashboard(self, _) -> None:
        if not self.daemon_up:
            rumps.alert("scriptorium", "Daemon is not running — start it first.")
            return
        subprocess.run(["open", _BASE + "/"], check=False)

    def open_settings(self, _) -> None:
        if not self.daemon_up:
            rumps.alert("scriptorium", "Daemon is not running — start it first.")
            return
        subprocess.run(["open", _BASE + "/settings"], check=False)

    def open_logs(self, _) -> None:
        subprocess.run(["open", str(LOG_DIR)], check=False)


def main() -> None:
    ScriptoriumMenubar().run()
