"""scriptorium command-line interface.

Diagnostic and operational commands for working outside the daemon: status,
single-PDF parsing, a full sync, and running the daemon itself. Listing,
inspecting and searching the library are the daemon's hub tools. Indexing
has exactly one write path — sync (the daemon's hub_sync uses the same
service code). The index allows one process at a time: sync refuses to run
while the daemon is up, since the daemon would later overwrite its changes
to the sync state.
"""

import json
import urllib.error
import urllib.request

import typer

from scriptorium import __version__
from scriptorium.config import load_config

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """scriptorium: local literature hub over a Zotero library."""


def zotero_reachable(api_base: str, timeout: float = 2.0) -> bool:
    """Probe the Zotero local HTTP API (requires the Zotero app to be running)."""
    try:
        with urllib.request.urlopen(api_base + "/", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # served an HTTP response, so the API is up
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def daemon_running(port: int, timeout: float = 1.0) -> bool:
    """Whether the scriptorium daemon answers on its port."""
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def refuse_if_daemon_running(port: int) -> None:
    if daemon_running(port):
        typer.echo("error: the scriptorium daemon is running and owns the index; use its"
                   " hub tools (hub_sync, hub_search_chunks) or stop it first", err=True)
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show configuration, index state, and Zotero reachability."""
    cfg = load_config()
    from scriptorium.store import STORE_FILE

    index_exists = (cfg.index_dir / STORE_FILE).exists()
    index_info: dict = {}
    if index_exists:
        # SQLite lets this read while the daemon writes.
        from scriptorium.index import HubIndex

        hub = HubIndex(cfg)
        counts = hub.counts()
        index_info = {
            "indexed_papers": counts["papers"],
            "indexed_chunks": counts["chunks"],
            "library_version_watermark": counts["library_version"],
            "failed_items": hub.state.failed,
        }
        hub.close()
    from pathlib import Path

    from scriptorium.settings import load_settings

    zotero_settings = load_settings(cfg)["zotero"]
    zotero_dir = Path(zotero_settings["data_dir"]).expanduser()
    info = {
        "version": __version__,
        "zotero_local_api": cfg.zotero_local_api,
        "zotero_running": zotero_reachable(cfg.zotero_local_api),
        "zotero_data_dir": str(zotero_dir),
        "zotero_data_dir_exists": zotero_dir.exists(),
        "linked_attachments_dir": zotero_settings["linked_attachments_dir"] or "(not set)",
        "index_dir": str(cfg.index_dir),
        "index": "present" if index_exists else "not yet built",
        "embedding_model": cfg.embedding_model,
        **index_info,
    }
    typer.echo(json.dumps(info, indent=2))


@app.command()
def parse(
    item_key: str,
    full: bool = typer.Option(False, help="Dump every passage in full"),
) -> None:
    """Read one paper's PDF into passages (GROBID; the TEI is kept) and show them."""
    from scriptorium.grobid import GrobidUnavailable
    from scriptorium.parsing import (
        ParseError,
        grobid_from_settings,
        md5sum,
        parse_paper,
        tei_store,
    )
    from scriptorium.zotero import ZoteroReader, ZoteroUnavailableError

    cfg = load_config()
    try:
        record = ZoteroReader(cfg).get_paper(item_key)
    except ZoteroUnavailableError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if record is None:
        typer.echo(f"error: no regular item with key {item_key!r}", err=True)
        raise typer.Exit(1)

    typer.echo(f"{record.citation}\nkey={record.citation_key or '(none)'}  pdf={record.pdf_path}\n")
    if record.pdf_status != "ok":
        typer.echo(f"error: no usable PDF (status: {record.pdf_status})", err=True)
        raise typer.Exit(1)
    try:
        chunks = parse_paper(record, md5sum(record.pdf_path), tei_store(cfg),
                             grobid_from_settings(cfg))
    except (ParseError, GrobidUnavailable) as exc:
        typer.echo(f"parse failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    words = sum(len(c.text.split()) for c in chunks)
    artefacts = sum(c.artefact for c in chunks)
    typer.echo(f"{len(chunks)} passages ({artefacts} artefacts), {words} words\n")
    for i, c in enumerate(chunks):
        label = (f"[{i:>3}] {c.part} pages {c.pages or '?'} ({len(c.text.split())} words)"
                 + (" ARTEFACT" if c.artefact else ""))
        if full:
            typer.echo(f"--- {label} ---\n{c.text}\n")
        else:
            typer.echo(f"{label}\n     {c.text[:300]}...\n")


@app.command()
def sync() -> None:
    """Full reconcile: make the index match Zotero.

    Anything with a usable PDF that is missing from the index (or changed) is
    ingested; deleted items are removed; unchanged items are skipped by
    version + content hash.

    Deletion detection: the local API stops listing an item the moment it is
    moved to the trash (no need to empty the trash), so a plain delete in the
    Zotero GUI is picked up on the next sync.
    """
    import time

    from scriptorium.index import HubIndex
    from scriptorium.service import sync_library
    from scriptorium.zotero import ZoteroReader, ZoteroUnavailableError

    cfg = load_config()
    refuse_if_daemon_running(cfg.port)
    reader = ZoteroReader(cfg)
    hub = HubIndex(cfg)
    t0 = time.time()
    try:
        summary = sync_library(reader, hub, cfg, log=typer.echo)
    except ZoteroUnavailableError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        hub.close()
    typer.echo(
        f"done in {time.time() - t0:.0f}s — added={summary['added']}"
        f" updated={summary['updated']} metadata={summary['metadata_updated']}"
        f" removed={summary['removed']} unchanged={summary['unchanged']}"
        f" unavailable={summary.get('unavailable', 0)}"
        f" failed={summary['failed']}"
    )


@app.command()
def serve(
    port: int = typer.Option(0, help="Port for the daemon (0 = config default)"),
) -> None:
    """Run the scriptorium daemon (MCP over HTTP on localhost)."""
    from scriptorium.server import run

    run(port=port or None)


if __name__ == "__main__":
    app()
