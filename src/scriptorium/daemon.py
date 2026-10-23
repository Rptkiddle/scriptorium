"""The daemon's state: the index, the embedding model, Zotero access and the
sync machinery, created once by start() when the daemon runs.

Concurrency: FastMCP runs the synchronous tool handlers in a thread pool,
so parallel calls are real threads. The embedder serializes model calls
internally (sub-batch granularity), a shared RLock guards the Qdrant
client, and write operations (sync/ingest) are additionally serialized by
one write lock, so at most one write runs at a time while searches
interleave between embedding batches.
"""

import threading
import time

from scriptorium.config import Config
from scriptorium.index import Embedder, HubIndex
from scriptorium.search import HubSearch
from scriptorium.service import catalogue_outdated, sync_library
from scriptorium.settings import load_settings
from scriptorium.zotero import ZoteroReader, ZoteroUnavailableError
from scriptorium.zotero_write import ZoteroWriter


def log(message: str) -> None:
    """A timestamped line in daemon.log (stdout, which launchd keeps)."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client_lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.embedder = Embedder(cfg.embedding_model, cfg.embedding_dim)
        self.hub = HubIndex(cfg, embedder=self.embedder, client_lock=self.client_lock)
        self.search = HubSearch(
            cfg,
            client_provider=lambda: self.hub.client,
            embedder=self.embedder,
            client_lock=self.client_lock,
        )
        self.reader = ZoteroReader(cfg)
        # A write through the hub wakes the auto-sync watcher, so the index
        # (which hub_item, hub_lookup and the searches read) catches up at once
        # instead of at the next timed check.
        self.sync_wanted = threading.Event()
        self.writer = ZoteroWriter(cfg, reader=self.reader, on_write=self.sync_wanted.set)

        # Live sync progress, readable via /health while a sync runs.
        self.sync_status_lock = threading.Lock()
        self.sync_status: dict | None = None
        self.sync_cancel = threading.Event()
        # Zotero's library version at the moment a sync was cancelled. While
        # the library stays at exactly this version, the auto-sync watcher
        # holds off, so a user's "Pause sync" is not undone at the next tick.
        # Cleared by any sync that runs to completion.
        self.sync_paused_at: int | None = None
        # The most recent sync failure ({"error", "at"}), cleared by any sync
        # that completes. Surfaced in /health so the menubar can show WHY the
        # index is behind instead of silently retrying (a stale TCC grant once
        # failed every auto-sync tick for days with no outward sign).
        self.last_sync_error: dict | None = None

    def sync_progress(self, done: int, total: int, current: str) -> None:
        with self.sync_status_lock:
            self.sync_status = {"done": done, "total": total, "current": current}

    def run_sync(self) -> dict:
        """One library sync under the write lock; shared by the hub_sync tool
        and the auto-sync watcher."""
        if not self.write_lock.acquire(blocking=False):
            return {"error": "a sync or ingest is already running"}
        try:
            self.sync_cancel.clear()
            with self.sync_status_lock:
                self.sync_status = {"done": 0, "total": 0, "current": "starting"}
            try:
                result = sync_library(
                    self.reader, self.hub, self.cfg,
                    log=log, progress=self.sync_progress, cancel=self.sync_cancel,
                )
            finally:
                with self.sync_status_lock:
                    self.sync_status = None
            # Record a pause while still holding the write lock, so the
            # watcher cannot start a new sync in the gap before it is set.
            if result.get("cancelled"):
                try:
                    self.sync_paused_at = self.reader.library_version()
                except ZoteroUnavailableError:
                    pass
            else:
                self.sync_paused_at = None
        except Exception as exc:  # noqa: BLE001 — record any failure, then report it
            log(f"sync failed: {exc}")
            self.last_sync_error = {
                "error": str(exc), "at": time.strftime("%Y-%m-%d %H:%M"),
            }
            return {"error": str(exc)}
        finally:
            self.write_lock.release()
        self.last_sync_error = None
        log("sync " + ("paused" if result.get("cancelled") else "done") + ": "
            + ", ".join(f"{k} {result[k]}" for k in
                        ("added", "updated", "metadata_updated", "removed", "failed")))
        return result

    def idle_reaper(self) -> None:
        """Free the embedding model and the embedded store after idle periods
        (general.model_idle_minutes / index_idle_minutes; 0 keeps them loaded)."""
        while True:
            time.sleep(60)
            try:
                general = load_settings(self.cfg)["general"]
                now = time.monotonic()
                model_idle = general["model_idle_minutes"]
                if (
                    model_idle
                    and self.embedder.loaded
                    and now - self.embedder.last_used > model_idle * 60
                ):
                    self.embedder.unload()
                index_idle = general["index_idle_minutes"]
                if (
                    index_idle
                    and self.hub.store_open
                    and now - self.hub.client_last_used > index_idle * 60
                    and not self.write_lock.locked()
                ):
                    self.hub.release_client()
            except Exception:  # noqa: BLE001 — the reaper must never die
                pass

    def auto_sync_watcher(self) -> None:
        """Start a sync whenever the index falls behind Zotero (general.auto_sync
        in settings; the interval is re-read every pass so changes apply live).
        A write through the hub (sync_wanted) starts the check at once."""
        while True:
            try:
                general = load_settings(self.cfg)["general"]
                self.sync_wanted.wait(general["auto_sync_interval"])
                if not self.watch_step(general):
                    time.sleep(5)
            except Exception:  # noqa: BLE001 — the watcher must never die
                time.sleep(60)

    def watch_step(self, general: dict) -> bool:
        """After a wait: False while a sync or ingest holds the write lock (any
        pending wake-up is kept, so a write made meanwhile is picked up as soon
        as the lock is free); otherwise one check_and_sync pass, True."""
        if self.write_lock.locked():
            return False
        self.check_and_sync(general)
        return True

    def check_and_sync(self, general: dict) -> None:
        """One watcher pass: sync if the index is behind Zotero."""
        self.sync_wanted.clear()
        if not general["auto_sync"]:
            return
        try:
            version = self.reader.library_version()
        except ZoteroUnavailableError:
            return
        # != rather than >: a lower Zotero version means its numbering
        # was reset (Zotero 10 local versions), which also needs a sync.
        # So does a catalogue built under older version stamps.
        behind = (version != self.hub.state.library_version
                  or catalogue_outdated(self.hub.state))
        paused = self.sync_paused_at is not None and version == self.sync_paused_at
        if behind and not paused:
            self.run_sync()


_current: Daemon | None = None


def start(cfg: Config) -> Daemon:
    """Create the daemon's state and start its background threads. Once per
    process."""
    global _current
    if _current is not None:
        raise RuntimeError("the daemon is already started")
    d = Daemon(cfg)
    threading.Thread(target=d.idle_reaper, daemon=True, name="idle-reaper").start()
    threading.Thread(target=d.auto_sync_watcher, daemon=True, name="auto-sync").start()
    _current = d
    return d


def current() -> Daemon:
    """The running daemon's state (for tools and routes)."""
    if _current is None:
        raise RuntimeError("the daemon is not running in this process (scriptorium serve)")
    return _current
