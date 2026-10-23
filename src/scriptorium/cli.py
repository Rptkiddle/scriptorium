"""scriptorium command-line interface.

Diagnostic and operational commands for working outside the daemon: status,
Zotero listing/inspection, single-PDF parsing, index search, a full sync,
and running the daemon itself. Indexing has exactly one write path — sync
(the daemon's hub_sync uses the same service code). The index allows one
process at a time: search and sync refuse to run while the daemon is up,
since the daemon would later overwrite their changes to the sync state.
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
    info = {
        "version": __version__,
        "zotero_local_api": cfg.zotero_local_api,
        "zotero_running": zotero_reachable(cfg.zotero_local_api),
        "zotero_data_dir": str(cfg.zotero_data_dir),
        "zotero_data_dir_exists": cfg.zotero_data_dir.exists(),
        "index_dir": str(cfg.index_dir),
        "index": "present" if index_exists else "not yet built",
        "embedding_model": cfg.embedding_model,
        **index_info,
    }
    typer.echo(json.dumps(info, indent=2))


@app.command()
def items(
    creator: str = typer.Option("", help="Filter by creator surname (case-insensitive)"),
    since: int = typer.Option(-1, help="Only items changed after this library version"),
    limit: int = typer.Option(0, help="Show at most N records (0 = all)"),
) -> None:
    """List papers from Zotero (metadata ground truth check)."""
    from scriptorium.zotero import ZoteroReader, ZoteroUnavailableError

    cfg = load_config()
    reader = ZoteroReader(cfg)
    try:
        records = reader.iter_papers(since=since if since >= 0 else None)
        version = reader.library_version()
    except ZoteroUnavailableError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if creator:
        needle = creator.lower()
        records = [r for r in records if needle in r.creators.lower()]

    shown = records[:limit] if limit else records
    for r in shown:
        marker = {"ok": "+", "no_pdf": "-", "not_downloaded": "~", "linked_missing": "!"}[r.pdf_status]
        typer.echo(f"[{marker}] {r.item_key}  {r.citation}")
    counts: dict[str, int] = {}
    for r in records:
        counts[r.pdf_status] = counts.get(r.pdf_status, 0) + 1
    typer.echo(
        f"\n{len(records)} papers (library version {version}) — pdf: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )
    typer.echo("legend: [+] pdf on disk  [~] not downloaded  [!] linked file missing  [-] no pdf")


@app.command()
def show(item_key: str) -> None:
    """Show the full PaperRecord for one Zotero item."""
    from scriptorium.zotero import ZoteroReader, ZoteroUnavailableError

    cfg = load_config()
    reader = ZoteroReader(cfg)
    try:
        record = reader.get_paper(item_key)
    except ZoteroUnavailableError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if record is None:
        typer.echo(f"error: no regular item with key {item_key!r}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(record.to_dict(), indent=2))


@app.command()
def parse(
    item_key: str,
    full: bool = typer.Option(False, help="Dump every chunk in full"),
) -> None:
    """Parse one paper's PDF into chunks and report text quality."""
    from scriptorium.parsing import ParseError, parse_paper
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
    try:
        chunks = parse_paper(record, cfg)
    except ParseError as exc:
        typer.echo(f"parse failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    total_chars = sum(len(c.text) for c in chunks)
    typer.echo(f"{len(chunks)} chunks, {total_chars} chars total\n")
    for i, c in enumerate(chunks):
        if full:
            typer.echo(f"--- {c.name} ({len(c.text)} chars) ---\n{c.text}\n")
        else:
            preview = " ".join(c.text[:300].split())
            typer.echo(f"[{i:>2}] {c.name} ({len(c.text)} chars)\n     {preview}...\n")


@app.command()
def search(
    query: str,
    k: int = typer.Option(10, help="Number of results"),
    full: bool = typer.Option(False, help="Print full chunk text instead of snippets"),
    creator: list[str] = typer.Option([], help="Filter: creator surname (repeatable)"),
    year: list[int] = typer.Option([], help="Filter: publication year (repeatable)"),
    year_from: int = typer.Option(None, help="Filter: earliest year (inclusive)"),
    year_to: int = typer.Option(None, help="Filter: latest year (inclusive)"),
    venue: str = typer.Option("", help="Filter: venue name (case-insensitive exact)"),
) -> None:
    """Search the index and print ranked chunks with citations."""
    from scriptorium.search import HubSearch

    filters: dict = {}
    if creator:
        filters["creators"] = list(creator)
    if year:
        filters["years"] = list(year)
    if year_from is not None:
        filters["year_from"] = year_from
    if year_to is not None:
        filters["year_to"] = year_to
    if venue:
        filters["venue"] = venue

    cfg = load_config()
    refuse_if_daemon_running(cfg.port)
    try:
        results = HubSearch(cfg).search(query, k=k, filters=filters or None)
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    for r in results:
        typer.echo(f"[{r.rank}] {r.name}  (sim={r.similarity:.3f})")
        if full:
            typer.echo(r.text + "\n")
        else:
            snippet = " ".join(r.text[:280].split())
            typer.echo(f"    {snippet}...\n")


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
