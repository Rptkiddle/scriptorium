"""The daemon's web routes: the dashboard, settings, nightly-run and
missing-PDF pages and their JSON, the latest digest, and /health. Imported
by server.run once the daemon has started."""

import urllib.request

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from scriptorium import __version__, dashboard_page, settings_page, surveillance_page
from scriptorium.daemon import current
from scriptorium.schedule import reconcile_nightly_schedule
from scriptorium.settings import (
    SettingsFileError,
    load_settings,
    mask_settings,
    save_settings,
)
from scriptorium.tools import mcp
from scriptorium.zotero import ZoteroUnavailableError


# Plain (non-async) route functions run in Starlette's thread pool, so a
# handler that waits on the index lock during a sync never blocks the event
# loop (and with it every other request and MCP call).
@mcp.custom_route("/health", methods=["GET"])
def health(_: Request) -> JSONResponse:
    """Liveness endpoint for the menubar companion and scripts."""
    d = current()
    with d.sync_status_lock:
        sync = dict(d.sync_status) if d.sync_status else None
    payload = {"status": "ok", "version": __version__, **d.hub.counts()}
    if sync:
        payload["sync"] = sync
    if d.last_sync_error:
        payload["last_sync_error"] = d.last_sync_error
    return JSONResponse(payload)


@mcp.custom_route("/", methods=["GET"])
async def dashboard(_: Request):
    return HTMLResponse(dashboard_page.PAGE)


@mcp.custom_route("/dashboard.json", methods=["GET"])
def dashboard_json(_: Request) -> JSONResponse:
    """Status data for the dashboard cards."""
    d = current()
    counts = d.hub.counts()
    zotero = {"reachable": False, "library_version": None, "index_behind": None}
    try:
        with urllib.request.urlopen(
            "http://localhost:23119/api/users/0/items?limit=1&format=versions",
            timeout=1.0,
        ) as r:
            live = int(r.headers.get("Last-Modified-Version", 0))
            zotero = {
                "reachable": True,
                "library_version": live,
                "index_behind": live != counts.get("library_version", 0),
            }
    except Exception:  # noqa: BLE001 — Zotero closed is a normal state
        pass
    return JSONResponse({
        "hub": counts,
        "zotero": zotero,
        "surveillance": surveillance_page.dashboard_card(d.cfg),
    })


@mcp.custom_route("/pdfs", methods=["GET"])
async def pdfs_page(_: Request):
    return HTMLResponse(dashboard_page.PDFS_PAGE)


@mcp.custom_route("/pdfs.json", methods=["GET"])
async def pdfs_json(_: Request) -> JSONResponse:
    """The missing-PDF pool, live from Zotero (regular items whose
    pdf_status is not ok)."""
    d = current()

    def _pool() -> dict:
        try:
            d.reader.check_available()
        except ZoteroUnavailableError as exc:
            return {"error": str(exc)}
        items = [
            {"key": r.item_key, "citation_key": r.citation_key,
             "title": r.title, "venue": r.venue, "doi": r.doi,
             "status": r.pdf_status}
            for r in d.reader.iter_papers() if r.pdf_status != "ok"
        ]
        by_status: dict = {}
        for it in items:
            by_status[it["status"]] = by_status.get(it["status"], 0) + 1
        return {"items": items, "by_status": by_status}

    return JSONResponse(await run_in_threadpool(_pool))


@mcp.custom_route("/digest", methods=["GET"])
async def latest_digest(_: Request):
    """The most recent morning digest, as plain text."""
    # Run folders are named YYYY-MM-DD-HHMMSS, so name order is time order.
    runs = current().cfg.index_dir / "surveillance" / "runs"
    digests = sorted(runs.glob("*/digest.md"))
    if not digests:
        return PlainTextResponse("no digests yet", status_code=404)
    return PlainTextResponse(digests[-1].read_text())


@mcp.custom_route("/surveillance", methods=["GET"])
async def surveillance_view(_: Request):
    return HTMLResponse(surveillance_page.PAGE)


@mcp.custom_route("/surveillance.json", methods=["GET"])
async def surveillance_json(request: Request) -> JSONResponse:
    """Run history plus one run's accepted papers and venue summary."""
    return JSONResponse(
        surveillance_page.runs_payload(current().cfg, request.query_params.get("run")))


@mcp.custom_route("/settings.json", methods=["GET"])
async def settings_json(_: Request) -> JSONResponse:
    """Current settings (DEFAULTS overlaid with the stored file); secret
    values are masked — the stored key never leaves the file."""
    return JSONResponse(mask_settings(load_settings(current().cfg)))


@mcp.custom_route("/settings", methods=["GET"])
async def settings_view(_: Request):
    return HTMLResponse(settings_page.PAGE)


@mcp.custom_route("/settings", methods=["POST"])
async def settings_save(request: Request) -> JSONResponse:
    cfg = current().cfg
    try:
        incoming = await request.json()
    except ValueError as exc:
        return JSONResponse({"error": f"invalid JSON: {exc}"}, status_code=400)
    try:
        saved = await run_in_threadpool(save_settings, cfg, incoming)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except SettingsFileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    try:
        await run_in_threadpool(
            reconcile_nightly_schedule,
            bool(saved["surveillance"]["enabled"]),
            saved["surveillance"]["nightly_hour"],
        )
    except Exception as exc:  # noqa: BLE001 — scheduling must not break saving
        return JSONResponse({**mask_settings(saved), "warning": f"schedule not updated: {exc}"})
    return JSONResponse(mask_settings(saved))
