"""Shared write-path operations (sync, targeted ingest).

One implementation used by both the CLI and the daemon, so their behaviour
cannot drift. Callers provide a log callback for progress lines.
"""

import datetime
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from scriptorium.config import Config
from scriptorium.grobid import Grobid, GrobidUnavailable
from scriptorium.index import (
    CATALOGUE_DOC_VERSION,
    CATALOGUE_PAYLOAD_VERSION,
    HubIndex,
    SyncState,
    folder_paths,
    folders_signature,
)
from scriptorium.parsing import (
    PARSER_VERSION,
    ParseError,
    PdfHashes,
    grobid_from_settings,
    parse_paper,
    read_pdf,
    tei_store,
)
from scriptorium.tei import TeiStore
from scriptorium.zotero import METADATA_VERSION, PaperRecord, ZoteroReader


def catalogue_outdated(state: SyncState) -> bool:
    """Whether the catalogue was built under older version stamps, so the
    next sync re-embeds it or rewrites its stored fields."""
    return (state.catalogue_doc != CATALOGUE_DOC_VERSION
            or state.catalogue_meta != METADATA_VERSION
            or state.catalogue_payload != CATALOGUE_PAYLOAD_VERSION)


GROBID_WORKERS = 4  # PDFs with GROBID at once (its config allows 10)


def read_new_pdfs(records: list[PaperRecord], hashes: dict[str, str], hub: HubIndex,
                  store: TeiStore, grobid: Grobid, log: Callable[[str], None],
                  progress: Callable[[int, int, str], None],
                  cancel: threading.Event | None) -> str:
    """Phase one of indexing: GROBID parses every PDF that has no stored TEI
    yet, a few at a time, before any embedding starts. A PDF GROBID rejects
    is recorded as a failure. Returns "" or, when GROBID cannot be started,
    the reason (those papers then wait for the next sync)."""
    todo = [r for r in records if not store.usable(r.item_key, hashes[r.item_key])]
    if not todo:
        return ""
    log(f"  reading {len(todo)} PDFs with GROBID")
    try:
        grobid.ensure()
    except GrobidUnavailable as exc:
        log(f"  GROBID unavailable: {exc}")
        return str(exc)

    def one(record: PaperRecord) -> tuple[PaperRecord, str]:
        if cancel is not None and cancel.is_set():
            return record, "cancelled"
        try:
            read_pdf(record, hashes[record.item_key], store, grobid)
            return record, ""
        except ParseError as exc:
            return record, exc.reason
        except GrobidUnavailable as exc:
            return record, f"GROBID unavailable: {exc}"
        except OSError as exc:
            return record, f"unreadable PDF: {exc}"

    unavailable = ""
    with ThreadPoolExecutor(max_workers=GROBID_WORKERS) as pool:
        for done, (record, error) in enumerate(pool.map(one, todo), 1):
            progress(done, len(todo), f"reading PDFs ({len(todo)})")
            if error.startswith("GROBID unavailable"):
                unavailable = unavailable or error
            elif error and error != "cancelled":
                hub.record_failure(record.item_key, error,
                                   f"{hashes[record.item_key]}:{PARSER_VERSION}")
                log(f"  FAIL {record.citation_key or record.item_key}: {error}")
    hub.save_state()
    return unavailable


def _utc_iso(ts: datetime.datetime) -> str:
    return ts.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def rebase_versions(
    state: SyncState, records: list[PaperRecord], current_version: int,
    last_sync_at: str,
) -> list[str] | None:
    """Adopt a reset Zotero version numbering without reprocessing.

    Zotero 10's local API reports local versions, much lower than the synced
    versions earlier releases returned, so stored versions cannot be compared
    with new ones. When Zotero's library version is below the stored
    watermark, every stored version is replaced by the item's new version,
    except for items modified after the last completed sync (and catalogue
    entries whose PDF presence no longer matches the full-text index): those
    stay stale, so the sync that follows processes them. Returns the keys
    left stale, or None if no reset happened. Chunk vectors are unaffected
    either way; they depend on PDF content, not versions.

    Known limitation: this trusts Zotero's dateModified. An item whose
    change left dateModified at or before last_sync_at (an edit made before
    a sync that then failed, or one applied with an older timestamp) is
    adopted as unchanged and is not reprocessed until it changes again.
    """
    if current_version >= state.library_version:
        return None
    stale: list[str] = []
    for r in records:
        unchanged = bool(last_sync_at and r.date_modified and r.date_modified <= last_sync_at)
        if not unchanged:
            stale.append(r.item_key)
            continue
        if r.item_key in state.items:
            state.items[r.item_key]["version"] = r.version
        pdf_matches = (r.pdf_status == "ok") == (r.item_key in state.items)
        if r.item_key in state.catalogue and pdf_matches:
            state.catalogue[r.item_key] = r.version
        elif r.item_key in state.catalogue:
            stale.append(r.item_key)
    state.library_version = current_version
    return stale


def sync_library(
    reader: ZoteroReader,
    hub: HubIndex,
    config: Config,
    log: Callable[[str], None] = lambda _: None,
    progress: Callable[[int, int, str], None] = lambda *_: None,
    cancel: threading.Event | None = None,
    grobid: Grobid | None = None,
) -> dict:
    """Full reconcile: make the index match Zotero.

    Enumerates the whole library — anything with a usable PDF that is missing
    from the index (or changed) is ingested; deleted items are removed.
    Idempotent: unchanged items are skipped by version + content hash + parser
    version, so routine syncs are fast and the first sync after a fresh index
    performs the complete initial build. State is saved after every write, so
    an interrupted or cancelled sync repeats at most one paper and resumes
    where it stopped on the next run.

    ``progress(done, total, current_key)`` is called once per record.
    Setting the ``cancel`` event stops the sync cleanly between papers; the
    returned summary then carries ``cancelled: True``.

    Full text is indexed in two phases: GROBID parses every new or changed
    PDF first (read_new_pdfs), then each paper's stored TEI is cut into
    passages and embedded. ``grobid`` is the caller's GROBID (the daemon
    keeps one and stops it when idle); without one, a sync starts its own,
    stopped when the process exits. If GROBID cannot be started, papers
    needing it wait for a later sync and the summary says why.
    """
    started = _utc_iso(datetime.datetime.now(datetime.UTC))
    current_version = reader.library_version()
    watermark = hub.state.library_version
    log(f"sync: library version {watermark} -> {current_version}")
    added = updated = metadata_updated = removed = failed = unchanged = unavailable = 0
    catalogued = 0
    cancelled = False

    records = reader.iter_papers()
    hashes = PdfHashes(hub.state_path.with_name("pdf_hashes.json"))
    store = tei_store(config)
    own_grobid = grobid is None  # started here: stopped once the PDFs are read
    grobid = grobid or grobid_from_settings(config)

    last_sync_at = hub.state.last_sync_at
    stale_after_reset = rebase_versions(hub.state, records, current_version, last_sync_at)
    if stale_after_reset is not None:
        hub.save_state()
        log(f"  Zotero version numbering reset ({watermark} -> {current_version}):"
            f" stored versions rebased; {len(stale_after_reset)} items changed"
            f" since {last_sync_at} will be processed")

    # Catalogue pass: item-level metadata index over the whole library,
    # PDFs or not. Batched — each item is one short document.
    # A CATALOGUE_DOC_VERSION change re-embeds every entry; a METADATA_VERSION
    # or CATALOGUE_PAYLOAD_VERSION change rewrites every entry's stored
    # fields and keeps its vector, and so does a renamed or moved folder
    # (item versions do not change when only their folders' names do). The
    # stamps are set only once a full catalogue pass completes.
    folders = folder_paths(reader.collections())
    folders_sig = folders_signature(folders)
    rebuild = hub.state.catalogue_doc != CATALOGUE_DOC_VERSION
    refresh = not rebuild and (hub.state.catalogue_meta != METADATA_VERSION
                               or hub.state.catalogue_payload != CATALOGUE_PAYLOAD_VERSION
                               or hub.state.catalogue_folders != folders_sig)
    stale = [r for r in records
             if rebuild or hub.state.catalogue.get(r.item_key) != r.version
             or hub.state.catalogue_pdf.get(r.item_key) != (r.pdf_status == "ok")]
    stale_keys = {r.item_key for r in stale}
    fields_only = [r for r in records if refresh and r.item_key not in stale_keys]
    for start in range(0, len(stale), 64):
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        batch = stale[start : start + 64]
        progress(start + len(batch), len(stale), f"catalogue ({len(stale)} items)")
        catalogued += hub.upsert_catalogue(batch, folders)
        hub.save_state()
    refreshed = 0
    for start in range(0, len(fields_only), 256):
        if cancelled or (cancel is not None and cancel.is_set()):
            cancelled = True
            break
        batch = fields_only[start : start + 256]
        progress(start + len(batch), len(fields_only), f"catalogue fields ({len(fields_only)} items)")
        refreshed += hub.refresh_catalogue_payloads(batch, folders)
    if (rebuild or refresh) and not cancelled:
        hub.state.catalogue_meta = METADATA_VERSION
        hub.state.catalogue_doc = CATALOGUE_DOC_VERSION
        hub.state.catalogue_payload = CATALOGUE_PAYLOAD_VERSION
        hub.state.catalogue_folders = folders_sig
        hub.save_state()
    if catalogued:
        log(f"  catalogued {catalogued} items")
    if refreshed:
        log(f"  rewrote the stored fields of {refreshed} catalogue items")

    # Phase one: which papers need (re)embedding, and GROBID parses their PDFs.
    to_embed: dict[str, str] = {}  # item key -> PDF hash
    for record in records:
        if cancelled or record.pdf_status != "ok":
            continue
        try:
            content_hash = hashes.of(record)
        except OSError:
            continue  # reported in the loop below
        attempt = f"{content_hash}:{PARSER_VERSION}"
        if (record.item_key in hub.state.failed
                and hub.state.failed_hash.get(record.item_key) == attempt):
            continue
        if hub.classify_change(record, content_hash) == "embed":
            to_embed[record.item_key] = content_hash
    grobid_unavailable = ""
    if to_embed and not cancelled:
        grobid_unavailable = read_new_pdfs(
            [r for r in records if r.item_key in to_embed], to_embed, hub, store, grobid,
            log, progress, cancel)
        if own_grobid:
            grobid.stop()  # 3-4 GB the embedding phase does not need
    waiting = 0

    for i, record in enumerate(records):
        if cancel is not None and cancel.is_set():
            cancelled = True
            log("sync: cancelled")
            break
        progress(i + 1, len(records), record.citation_key or record.item_key)
        tracked = record.item_key in hub.state.items
        if record.pdf_status != "ok":
            if tracked and record.pdf_status != "no_pdf":
                # The attachment exists but its file does not resolve right
                # now (Papers folder moved or unmounted, file not downloaded).
                # Keep the chunks: deleting would wipe the index wholesale
                # whenever the PDF store is unavailable.
                unavailable += 1
                continue
            if tracked:
                hub.delete_paper(record.item_key)
                store.remove(record.item_key)
                hub.save_state()
                removed += 1
                log(f"  removed {record.citation_key or record.item_key} (PDF attachment removed)")
            elif record.item_key in hub.state.failed:
                # A failed item whose PDF was since removed: the failure is moot.
                hub.state.failed.pop(record.item_key, None)
                hub.save_state()
                log(f"  cleared failure {record.citation_key or record.item_key} (PDF gone)")
            continue
        try:
            content_hash = hashes.of(record)
            attempt = f"{content_hash}:{PARSER_VERSION}"
            if (record.item_key in hub.state.failed
                    and hub.state.failed_hash.get(record.item_key) == attempt):
                failed += 1  # same PDF, same parser: it would fail again
                continue
            change = hub.classify_change(record, content_hash)
            if change == "none":
                unchanged += 1
                continue
            if change == "metadata":
                n = hub.update_metadata(record)
                hub.save_state()
                metadata_updated += 1
                log(f"  metadata {record.citation_key or record.item_key} ({n} chunks)")
                continue
            if not store.usable(record.item_key, content_hash):
                if grobid_unavailable:
                    waiting += 1  # GROBID could not read it this time
                    continue
                if record.item_key in hub.state.failed:
                    failed += 1  # recorded in phase one
                    continue
            n = hub.add_paper(record, parse_paper(record, content_hash, store, grobid),
                              content_hash)
            hub.save_state()
            if tracked:
                updated += 1
                log(f"  updated {record.citation_key or record.item_key} ({n} chunks)")
            else:
                added += 1
                log(f"  added {record.citation_key or record.item_key} ({n} chunks)")
        except ParseError as exc:
            failed += 1
            hub.record_failure(record.item_key, exc.reason, attempt)
            hub.save_state()
            log(f"  FAIL {exc}")
        except OSError as exc:
            # One unreadable file (permissions, iCloud-evicted) must not
            # abort the sync for every later item.
            failed += 1
            hub.record_failure(record.item_key, f"unreadable PDF: {exc}")
            hub.save_state()
            log(f"  FAIL {record.citation_key or record.item_key}: unreadable PDF: {exc}")

    if unavailable:
        log(f"  kept {unavailable} indexed papers whose PDF file does not resolve"
            " (attachment still present; check the PDF store)")
    if not cancelled:
        live_keys = reader.all_item_keys()
        for gone in set(hub.state.items) - live_keys:
            hub.delete_paper(gone)
            store.remove(gone)
            removed += 1
            log(f"  removed {gone} (deleted in Zotero)")
        for gone in set(hub.state.catalogue) - live_keys:
            hub.delete_catalogue(gone)
            log(f"  decatalogued {gone} (deleted in Zotero)")
        for gone in set(hub.state.failed) - live_keys:
            hub.state.failed.pop(gone, None)
            log(f"  cleared failure {gone} (deleted in Zotero)")
        # The watermark advances only on a completed pass; after a cancel the
        # next sync re-enumerates from the same baseline.
        hub.state.library_version = current_version
        hub.state.last_sync_at = started

    if waiting:
        log(f"  {waiting} papers wait for GROBID ({grobid_unavailable})")
    hub.save_state()
    hashes.save(prune=not cancelled)
    return {
        "added": added,
        "updated": updated,
        "metadata_updated": metadata_updated,
        "catalogued": catalogued,
        "removed": removed,
        "unchanged": unchanged,
        "unavailable": unavailable,
        "failed": failed,
        "cancelled": cancelled,
        "library_version": hub.state.library_version,
    } | ({"grobid_unavailable": grobid_unavailable, "waiting_for_grobid": waiting}
         if grobid_unavailable else {})


def ingest_items(
    reader: ZoteroReader,
    hub: HubIndex,
    config: Config,
    item_keys: list[str],
    log: Callable[[str], None] = lambda _: None,
    grobid: Grobid | None = None,
) -> dict:
    """Targeted ingest of specific Zotero items by key.

    Every item is (re)catalogued; full-text indexing additionally runs for
    items with a usable PDF.
    """
    reader.check_available()
    hashes = PdfHashes(hub.state_path.with_name("pdf_hashes.json"))
    store = tei_store(config)
    grobid = grobid or grobid_from_settings(config)  # stopped at process exit
    ingested, metadata_updated, skipped, catalogued, errors = [], [], [], [], {}
    folders = None
    for key in item_keys:
        record = reader.get_paper(key)
        if record is None:
            errors[key] = "no regular item with this key"
            continue
        if (hub.state.catalogue.get(record.item_key) != record.version
                or hub.state.catalogue_meta != METADATA_VERSION
                or hub.state.catalogue_doc != CATALOGUE_DOC_VERSION
                or hub.state.catalogue_payload != CATALOGUE_PAYLOAD_VERSION
                or hub.state.catalogue_pdf.get(record.item_key) != (record.pdf_status == "ok")):
            if folders is None:
                folders = folder_paths(reader.collections())
            hub.upsert_catalogue([record], folders)
            catalogued.append(record.citation_key or key)
        if record.pdf_status != "ok":
            skipped.append(f"{record.citation_key or key} (catalogue only: {record.pdf_status})")
            continue
        try:
            content_hash = hashes.of(record)
            change = hub.classify_change(record, content_hash)
            if change == "none":
                chunks = hub.state.items.get(record.item_key, {}).get("chunks", 0)
                skipped.append(f"{record.citation_key or key} (already indexed, {chunks} chunks)")
                continue
            if change == "metadata":
                n = hub.update_metadata(record)
                metadata_updated.append(f"{record.citation_key or key} ({n} chunks)")
                log(f"  metadata {record.citation_key or key} ({n} chunks)")
                continue
            n = hub.add_paper(record, parse_paper(record, content_hash, store, grobid),
                              content_hash)
            ingested.append(f"{record.citation_key or key} ({n} chunks)")
            log(f"  ingested {record.citation_key or key} ({n} chunks)")
        except GrobidUnavailable as exc:
            errors[key] = f"GROBID unavailable: {exc} (indexed at the next sync once it starts)"
        except ParseError as exc:
            errors[key] = exc.reason
            hub.record_failure(key, exc.reason, f"{content_hash}:{PARSER_VERSION}")
        except OSError as exc:
            errors[key] = f"unreadable PDF: {exc}"
            hub.record_failure(key, errors[key])
    hub.save_state()
    hashes.save()
    return {
        "ingested": ingested,
        "metadata_updated": metadata_updated,
        "catalogued": catalogued,
        "skipped": skipped,
        "errors": errors,
    }
