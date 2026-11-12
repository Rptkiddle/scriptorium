"""The API: one request per client tool (Table 1 of the README), under /v1.
Nothing is served without a member's API key.

Every request goes through auth.handle(), which
authenticates, applies the limits and writes the usage log. The request
bodies are checked against the models below; a field that does not exist
is refused, so a mistyped input is never silently ignored.
"""

import hashlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from scriptorium_server import __version__, auth, files, members, retrieval
from scriptorium_server.filestore import FileStore
from scriptorium_server.embedding import Embedder
from scriptorium_server.settings import Settings

API_VERSION = 1


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filters(_Body):
    authors: list[str] | None = Field(None, description="Any of these authors; any spelling of a surname.")
    authors_all: list[str] | None = Field(None, description="All of these authors together.")
    years: list[int] | None = None
    year_from: int | None = None
    year_to: int | None = None
    venues: list[str] | None = Field(None, description="Venue names as `facets` gives them.")
    publishers: list[str] | None = None
    categories: list[str] | None = None
    types: list[str] | None = Field(None, description="journalArticle, conferencePaper, preprint, ...")
    has_fulltext: bool | None = None
    added_since: str | None = Field(None, description="YYYY-MM-DD.")
    provenance: list[str] | None = Field(None, description="venue, donation, activity.")
    item_keys: list[str] | None = Field(None, description="Only these items (a set sent back).")


class LookupBody(_Body):
    inputs: list[str] = Field(description="DOIs, arXiv IDs or exact titles.")


class ListBody(_Body):
    filters: Filters | None = None
    count: int = Field(20, ge=0)
    offset: int = Field(0, ge=0)
    order: str = "added"
    fields: list[str] | None = None


class SearchItemsBody(_Body):
    query: str
    filters: Filters | None = None
    count: int = Field(20, ge=1)
    fields: list[str] | None = None
    phrase: str | None = Field(None, description="An exact phrase the title or abstract must hold.")


class SearchPassagesBody(_Body):
    query: str
    filters: Filters | None = None
    count: int = Field(10, ge=1)
    phrase: str | None = Field(None, description="An exact phrase the passage must hold.")


class FacetsBody(_Body):
    field: str
    filters: Filters | None = None
    contains: str = ""
    count: int = Field(50, ge=1)


class BibtexBody(_Body):
    keys: list[str]
    format: str = "biblatex"
    abstracts: bool = False


def _filters(filters: Filters | None) -> dict:
    return filters.model_dump(exclude_none=True) if filters else {}


def create_app(connect: auth.Connect, embedder: Embedder, settings: Settings,
               registry: retrieval.Registry | None = None) -> FastAPI:
    """The web service. `connect()` gives a database connection as a
    context manager; `registry` is asked for identifiers not held."""
    app = FastAPI(title="Scriptorium", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)
    per_request = int(settings["limits.per_request"])
    per_minute = int(settings["limits.requests_per_minute"])
    upload_bytes = int(settings["limits.upload_megabytes"]) * 1024 * 1024
    data = Path(str(settings["data.folder"]))
    store = FileStore(data)

    def serve(request: Request, name: str, inputs: dict, run) -> Any:
        return auth.handle(connect, request, name, inputs, run, per_minute)

    @app.get("/v1/health")
    def health(request: Request) -> Any:
        """Whether the server answers, and the size of the collection."""
        def run(conn, member):
            return {"status": "ok", "server": __version__, "api": API_VERSION,
                    "min_client": ".".join(map(str, auth.MIN_CLIENT)),
                    **retrieval.collection_size(conn)}
        return serve(request, "health", {}, run)

    @app.post("/v1/lookup")
    def lookup(body: LookupBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(len(body.inputs), per_request, "lookups")
            return retrieval.lookup(conn, body.inputs, member["id"], registry)
        return serve(request, "lookup", body.model_dump(), run)

    @app.post("/v1/items")
    def list_items(body: ListBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(body.count, per_request)
            return retrieval.list_items(conn, _filters(body.filters), body.count, body.offset,
                                        body.order, body.fields)
        return serve(request, "items", body.model_dump(exclude_none=True), run)

    @app.post("/v1/search/items")
    def search_items(body: SearchItemsBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(body.count, per_request)
            return retrieval.search_items(conn, embedder, body.query, _filters(body.filters),
                                          body.count, body.fields, body.phrase)
        return serve(request, "search/items", body.model_dump(exclude_none=True), run)

    @app.post("/v1/search/passages")
    def search_passages(body: SearchPassagesBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(body.count, per_request)
            return retrieval.search_passages(conn, embedder, body.query, _filters(body.filters),
                                             body.count, body.phrase)
        return serve(request, "search/passages", body.model_dump(exclude_none=True), run)

    @app.get("/v1/items/{key}")
    def item(key: str, request: Request) -> Any:
        def run(conn, member):
            found = retrieval.item(conn, key)
            if found is None:
                raise auth.Refused(404, f"no item with key or citation key {key!r}")
            return found
        return serve(request, "item", {"key": key}, run)

    @app.post("/v1/facets")
    def facets(body: FacetsBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(body.count, per_request, "values")
            return retrieval.facets(conn, body.field, _filters(body.filters), body.contains,
                                    body.count)
        return serve(request, "facets", body.model_dump(exclude_none=True), run)

    @app.post("/v1/bibtex")
    def bibtex(body: BibtexBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(len(body.keys), per_request, "entries")
            return retrieval.bibtex(conn, body.keys, body.format, body.abstracts)
        return serve(request, "bibtex", body.model_dump(), run)

    # -- files ---------------------------------------------------------------------
    # A PDF never enters the usage log: uploads and downloads are logged by
    # identifier, size and hash, and the list of held PDFs by its length.

    @app.post("/v1/files")
    async def donate(request: Request,
                     identifier: str | None = Query(None, description="The PDF's DOI or arXiv ID."),
                     item: str | None = Query(None, description="Or the key of the item it is for.")
                     ) -> Any:
        """Donate a PDF (the request body, application/pdf). Replies at once;
        the PDF is processed in the background (donation progress)."""
        # Nothing is read before the key is known and the declared size is
        # within the limit; a body without a declared size is read only up
        # to the limit.
        inputs: dict = {"identifier": identifier, "item": item}
        declared = request.headers.get("content-length", "")
        too_big = declared.isdigit() and int(declared) > upload_bytes
        body = b""
        if not too_big and await run_in_threadpool(known_key, request):
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > upload_bytes:
                    too_big = True
                    break
                chunks.append(chunk)
            body = b"".join(chunks)
            inputs.update(bytes=size, sha256=hashlib.sha256(body).hexdigest())

        def run(conn, member):
            if too_big:
                raise auth.Refused(413, f"the PDF is larger than {upload_bytes // 1048576} MB,"
                                        " the largest a member may donate")
            try:
                return files.receive(conn, body, member["id"], data / "holding", identifier, item)
            except ValueError as exc:
                raise auth.Refused(400, str(exc)) from exc
        return await run_in_threadpool(serve, request, "files/donate", inputs, run)

    def known_key(request: Request) -> bool:
        with connect() as conn:
            return members.authenticate(conn, auth._key(request)) is not None

    @app.get("/v1/files/held")
    def held(request: Request) -> Any:
        """Every item with a held PDF (key, DOI, arXiv ID, MD5 of each PDF as
        donated), for comparing locally before donating."""
        def run(conn, member):
            reply = files.held(conn)
            response = JSONResponse(reply)
            response.logged = {"count": reply["count"]}
            return response
        return serve(request, "files/held", {}, run)

    @app.get("/v1/donations")
    def donations(request: Request, count: int = Query(50, ge=1), offset: int = Query(0, ge=0)) -> Any:
        """The member's own donations, newest first, with their state."""
        def run(conn, member):
            auth.check_limit(count, per_request, "donations")
            return files.donations(conn, member["id"], count, offset)
        return serve(request, "donations", {"count": count, "offset": offset}, run)

    @app.get("/v1/files/{key}")
    def download(key: str, request: Request) -> Any:
        """The held PDF of an item (key or citation key)."""
        def run(conn, member):
            try:
                found = files.held_file(conn, key, store)
            except files.NotHeld as exc:
                raise auth.Refused(404, str(exc)) from exc
            name = f"{found['citation_key'] or found['key']}.pdf"
            response = FileResponse(found["path"], media_type="application/pdf", filename=name,
                                    headers={"X-Scriptorium-SHA256": found["sha256"]})
            response.logged = {"key": found["key"], "sha256": found["sha256"],
                               "bytes": found["bytes"]}
            return response
        return serve(request, "files/download", {"key": key}, run)

    @app.exception_handler(Exception)
    def unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": "the server could not handle this request; its"
                                      " administrator can find the cause in the usage log"},
                            status_code=500)

    return app
