"""Finding PDFs through Zotero's Find Available PDF lookup, for many items.

Requests start at the pace set in the settings page, whether or not earlier
ones have finished, and at most find_concurrency run at once. Items are
ordered round-robin by publisher (a DOI's registrant prefix, else the URL's
host) to keep requests to one publisher as far apart as the batch allows.
ZoteroWriter.find_pdf handles each item. Waiting on an item stops at
find_timeout_seconds, but Zotero may carry on with that lookup, so after
timeouts more lookups can be running inside Zotero than the limit here.
"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from scriptorium.config import Config
from scriptorium.settings import load_settings
from scriptorium.zotero import ZoteroReader
from scriptorium.zotero_write import ZoteroWriter


def publisher_group(data: dict) -> str:
    """What to spread requests over: a DOI's registrant prefix (one per
    publisher, near enough), else the host of the item's URL."""
    doi = (data.get("DOI") or "").strip().lower()
    if "/" in doi:
        return doi.split("/", 1)[0]
    return (urlparse(data.get("url") or "").hostname or "").lower()


def round_robin(groups: dict[str, str]) -> list[str]:
    """Item keys (given as {key: group}), one from each group in turn."""
    queues: dict[str, list[str]] = {}
    for key, group in groups.items():
        queues.setdefault(group, []).append(key)
    ordered = []
    while queues:
        for group in list(queues):
            ordered.append(queues[group].pop(0))
            if not queues[group]:
                del queues[group]
    return ordered


def find_pdfs(config: Config, keys: list[str], source: str | None = None,
              on_result: Callable[[str, dict], None] | None = None,
              writer: ZoteroWriter | None = None, reader: ZoteroReader | None = None,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> dict[str, dict]:
    """Look for each item's PDF; returns {key: result} in the order given."""
    settings = load_settings(config)["zotero"]
    reader = reader or ZoteroReader(config)
    writer = writer or ZoteroWriter(config, reader=reader)
    results: dict[str, dict] = {}
    report_lock = threading.Lock()

    def record(key: str, result: dict) -> None:
        with report_lock:
            results[key] = result
            if on_result is not None:
                on_result(key, result)

    groups: dict[str, str] = {}
    for key in dict.fromkeys(keys):
        try:
            data = reader.item_raw(key)
        except Exception as exc:  # noqa: BLE001 — e.g. Zotero closed
            record(key, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            continue
        if data is None:
            record(key, {"status": "failed", "error": f"no item with key {key}"})
        elif data.get("itemType") == "preprint" and not settings["find_include_preprints"]:
            record(key, {"status": "skipped", "reason": "a preprint"})
        else:
            groups[key] = publisher_group(data)

    concurrency = settings["find_concurrency"]
    slots = threading.Semaphore(concurrency)

    def work(key: str) -> None:
        try:
            result = writer.find_pdf(key, source=source)
        except Exception as exc:  # noqa: BLE001 — one item never stops the batch
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            slots.release()
        record(key, result)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        last_start = None
        for key in round_robin(groups):
            slots.acquire()
            if last_start is not None:
                wait = last_start + settings["find_interval_seconds"] - clock()
                if wait > 0:
                    sleep(wait)
            last_start = clock()
            pool.submit(work, key)
    return {key: results[key] for key in dict.fromkeys(keys)}


def count_statuses(results: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results.values():
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    return counts
