"""The API: one request per client tool (Table 1 of the README), under /v1.
Nothing is served without a member's API key or login, except /v1/login,
which tells a client where members log in, the web page at /, which holds
no data, and the page's sign-in under /auth: members sign in there through
the same login provider and the page then sends its session with each
request (see web.py).

Every request goes through auth.handle(), which
authenticates, applies the limits and writes the usage log. The request
bodies are checked against the models below; a field that does not exist
is refused, so a mistyped input is never silently ignored.
"""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from scriptorium_server import __version__, adminmenu, auth, files, members, oidc, retrieval, web
from scriptorium_server import login as member_login
from scriptorium_server import collections as member_collections
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
    provenance: list[str] | None = Field(None, description="venue, pdf_upload, activity.")
    item_keys: list[str] | None = Field(None, description="Only these items (a set sent back).")
    collections: list[str] | None = Field(
        None, description="Only items in any of these collections of the member (number or name).")
    text: str | None = Field(
        None, description='Words the item must hold, matched as whole words in any case:'
                          ' "a phrase" in quotes, `or` between alternatives, -word to exclude.')
    text_in: str | None = Field(
        None, description="Where the words of `text` are looked for: all (title, abstract,"
                          " authors, venue; the default), title, abstract, authors or venue.")


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


class CollectionsListBody(_Body):
    kind: str | None = Field(None, description="history or active; none for both.")
    contains: str = Field("", description="A part of the name or of what was asked.")
    count: int = Field(50, ge=1)
    offset: int = Field(0, ge=0)


class CollectionShowBody(_Body):
    collection: str = Field(description="The collection's number, or an active one's name.")
    count: int = Field(50, ge=0)
    offset: int = Field(0, ge=0)
    fields: list[str] | None = None


class CollectionAddBody(_Body):
    collection: str = Field(description="Number or name; a new name makes a new collection.")
    keys: list[str] = Field(default_factory=list, description="Item keys or citation keys.")
    from_collection: str | None = Field(None, description="Add every item of this collection too.")


class CollectionRecordBody(_Body):
    workflow: str = Field("", description="The workflow that ran: search, discovery, ...")
    name: str = Field("", description="A short name for the run, e.g. its question.")
    request: dict | None = Field(None, description="What was asked: query, filters, question.")
    keys: list[str] = Field(default_factory=list, description="The items found, best first.")
    collection: str | None = Field(None, description="Continue this history with more items.")


class CollectionRemoveBody(_Body):
    collection: str
    keys: list[str]


class CollectionEditBody(_Body):
    collection: str
    name: str | None = None
    keep: bool = Field(False, description="Turn a history into an active collection.")
    delete: bool = False


class AdminSettingsBody(_Body):
    name: str | None = Field(None, description="A setting's name; none for all of them.")
    value: str | None = Field(None, description="The new value; none to read the setting.")
    confirm: bool = Field(False, description="Make the change the preview showed.")


class AdminMembersBody(_Body):
    action: str = Field("list", description="list, add or revoke.")
    name: str | None = None
    confirm: bool = False


class AdminVenuesBody(_Body):
    action: str = Field("list", description="list, add or remove.")
    name: str | None = None
    contains: str = Field("", description="list: a part of the venue's or publisher's name.")
    source_id: str | None = Field(None, description="add: the venue's OpenAlex source ID.")
    issns: list[str] | None = None
    publisher: str | None = None
    category: str | None = Field(None, description="Journals, Proceedings, Preprints, ...")
    needs_filter: bool = Field(False, description="add: admit only its social-science papers.")
    count: int = Field(50, ge=1)
    offset: int = Field(0, ge=0)
    confirm: bool = False


class AdminRunsBody(_Body):
    action: str = Field("list", description="list or start.")
    kind: str | None = Field(None, description="surveillance or backup.")
    dry_run: bool = Field(False, description="start: a harvest that writes nothing.")
    count: int = Field(10, ge=1)
    confirm: bool = False


class AdminJobsBody(_Body):
    action: str = Field("list", description="list, show or retry.")
    status: str | None = Field(None, description="list: waiting, running, done or failed.")
    job_id: int | None = None
    count: int = Field(20, ge=1)
    confirm: bool = False


def _filters(filters: Filters | None, conn=None, member: dict | None = None) -> dict:
    """The filters as retrieval takes them. `collections` names the
    member's own collections and becomes the keys of their items."""
    chosen = filters.model_dump(exclude_none=True) if filters else {}
    named = chosen.pop("collections", None)
    if named:
        try:
            inside = member_collections.keys(conn, member["id"], named)
        except member_collections.NotFound as exc:
            raise auth.Refused(404, str(exc)) from exc
        if "item_keys" in chosen:
            inside = sorted(set(inside) & set(chosen["item_keys"]))
        chosen["item_keys"] = inside or [""]  # an empty collection matches nothing
    return chosen


def _logged(reply: dict, ranking: list) -> JSONResponse:
    """The reply as it is, with the returned items' keys, ranks and scores
    added in the usage log only."""
    response = JSONResponse(reply)
    response.logged = {**reply, "ranking": ranking}
    return response


def create_app(connect: auth.Connect, embedder: Embedder, settings: Settings,
               registry: retrieval.Registry | None = None,
               current: Callable[[], Settings] | None = None,
               login_fetch: Callable[[str], dict] | None = None,
               provider: oidc.Provider | None = None) -> FastAPI:
    """The web service. `connect()` gives a database connection as a
    context manager; `registry` is asked for identifiers not held;
    `current()` gives the settings in force now, for the settings that take
    effect while the server runs (without it, those given at the start);
    `login_fetch(address)` reads the login provider's public pages;
    `provider` is the web page's client at that provider (without it, the
    one login.issuer and the web settings name, if they name one)."""
    app = FastAPI(title="Scriptorium", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)
    data = Path(str(settings["data.folder"]))
    store = FileStore(data)

    def live() -> Settings:
        """The settings in force now: the limits and switches take effect
        while the server runs."""
        return current() if current else settings

    def limit(name: str) -> int:
        return int(live()[f"limits.{name}"])

    login = member_login.Login(live, login_fetch or member_login.fetch_json)

    def serve(request: Request, name: str, inputs: dict, run) -> Any:
        return auth.handle(connect, request, name, inputs, run, limit("requests_per_minute"),
                           login)

    def switched_on(name: str) -> bool:
        return live()[name] == "on"

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def start_page() -> str:
        """What Scriptorium is; the dashboard once a member signs in."""
        return web.page()

    # -- the web page's sign-in (oidc.py, web.py) ----------------------------------

    address = str(settings["web.address"]).rstrip("/")
    secure = not address.startswith("http://")  # cookies over HTTPS only, but on a test address
    providers: dict[str, oidc.Provider] = {}

    def web_provider() -> oidc.Provider | None:
        """The web page's client at the login provider in force now (the
        issuer is login.issuer, shared with the client's login); None when
        signing in to the page is not set up."""
        if not login.issuer:
            return None
        if provider is not None:
            return provider
        if not (address and settings["web.client_id"]):
            return None
        if login.issuer not in providers:
            providers[login.issuer] = oidc.Provider(
                login.issuer, str(settings["web.client_id"]), str(settings["web.client_secret"]),
                f"{address}/auth/callback")
        return providers[login.issuer]

    def signin_failed(message: str) -> RedirectResponse:
        response = RedirectResponse("/#signin-error=" + quote(message), status_code=303)
        response.delete_cookie(web.LOGIN_COOKIE, path="/auth")
        return response

    @app.get("/auth/login", include_in_schema=False)
    def web_login() -> Any:
        """Send the browser to the provider's login page."""
        provider = web_provider()
        if provider is None:
            return signin_failed("signing in is not set up on this server yet")
        verifier, challenge = oidc.pkce_pair()
        with connect() as conn:
            state, nonce = web.start_login(conn, verifier)
            conn.commit()
        try:
            url = provider.login_url(state, nonce, challenge)
        except oidc.SignInFailed as exc:
            return signin_failed(str(exc))
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(web.LOGIN_COOKIE, state, max_age=web.LOGIN_MINUTES * 60, path="/auth",
                            httponly=True, secure=secure, samesite="lax")
        return response

    @app.get("/auth/callback", include_in_schema=False)
    def callback(request: Request, code: str = "", state: str = "", error: str = "",
                 error_description: str = "") -> Any:
        """The provider sends the browser back here: exchange the code,
        find or make the member, start a session."""
        provider = web_provider()
        if provider is None:
            return signin_failed("signing in is not set up on this server yet")
        if error:
            return signin_failed(f"the sign-in was refused: {error_description or error}")
        if not state or request.cookies.get(web.LOGIN_COOKIE) != state:
            return signin_failed("this sign-in was not started in this browser; try again")
        with connect() as conn:
            pending = web.take_login(conn, state)
            conn.commit()
        if pending is None:
            return signin_failed("the sign-in took too long; try again")
        try:
            claims = provider.exchange(code, pending["verifier"], pending["nonce"])
        except oidc.SignInFailed as exc:
            return signin_failed(str(exc))
        hours = int(live()["web.session_hours"])
        with connect() as conn:
            member = members.for_login(conn, login.issuer, claims["sub"])
            conn.commit()
            if member["revoked_at"]:
                return signin_failed("your Scriptorium membership has been revoked")
            token = web.start_session(conn, member["id"], hours, web.shown_name(claims))
            conn.commit()
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(web.LOGIN_COOKIE, path="/auth")
        response.set_cookie(web.SESSION_COOKIE, token, max_age=hours * 3600, path="/",
                            httponly=True, secure=secure, samesite="lax")
        return response

    def signed_in_request(request: Request) -> bool:
        return request.headers.get(web.WEB_HEADER) == "1"

    @app.get("/auth/me", include_in_schema=False)
    def me(request: Request) -> Any:
        """Who is signed in, for the page's header."""
        member = None
        if signed_in_request(request):
            with connect() as conn:
                member = web.session_member(conn, request.cookies.get(web.SESSION_COOKIE, ""))
        if member is None:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        return {"name": member["shown_name"] or member["name"], "admin": bool(member["admin"])}

    @app.post("/auth/logout", include_in_schema=False)
    def logout(request: Request) -> Any:
        if not signed_in_request(request):
            return JSONResponse({"error": "sign out from the web page"}, status_code=403)
        with connect() as conn:
            web.end_session(conn, request.cookies.get(web.SESSION_COOKIE, ""))
            conn.commit()
        response = Response(status_code=204)
        response.delete_cookie(web.SESSION_COOKIE, path="/")
        return response

    @app.get("/v1/contents")
    def contents(request: Request) -> Any:
        """The number of items and of venues with items, for the web page."""
        return serve(request, "contents", {}, lambda conn, member: web.contents(conn))

    @app.get("/v1/health")
    def health(request: Request) -> Any:
        """Whether the server answers, and the size of the collection."""
        def run(conn, member):
            return {"status": "ok", "server": __version__, "api": API_VERSION,
                    "min_client": ".".join(map(str, auth.MIN_CLIENT)),
                    "pdf_download": switched_on("files.pdf_download"),
                    "pdf_upload": switched_on("files.pdf_upload"),
                    "admin": bool(member.get("admin")),
                    **retrieval.collection_size(conn)}
        return serve(request, "health", {}, run)

    @app.get("/v1/login")
    def login_details() -> Any:
        """Where members log in: the provider's address and the client id
        (both empty when the server uses API keys only). Served without a
        key, since a client needs it before the member can log in."""
        return login.described()

    @app.post("/v1/lookup")
    def lookup(body: LookupBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(len(body.inputs), limit("per_request"), "lookups")
            return retrieval.lookup(conn, body.inputs, member["id"], registry)
        return serve(request, "lookup", body.model_dump(), run)

    @app.post("/v1/items")
    def list_items(body: ListBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(body.count, limit("per_request"))
            ranking: list = []
            reply = retrieval.list_items(conn, _filters(body.filters, conn, member), body.count,
                                         body.offset, body.order, body.fields, ranking)
            return _logged(reply, ranking)
        return serve(request, "items", body.model_dump(exclude_none=True), run)

    @app.post("/v1/search/items")
    def search_items(body: SearchItemsBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(body.count, limit("per_request"))
            ranking: list = []
            reply = retrieval.search_items(conn, embedder, body.query,
                                           _filters(body.filters, conn, member), body.count,
                                           body.fields, body.phrase, ranking)
            return _logged(reply, ranking)
        return serve(request, "search/items", body.model_dump(exclude_none=True), run)

    @app.post("/v1/search/passages")
    def search_passages(body: SearchPassagesBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(body.count, limit("per_request"))
            return retrieval.search_passages(conn, embedder, body.query,
                                             _filters(body.filters, conn, member), body.count,
                                             body.phrase)
        return serve(request, "search/passages", body.model_dump(exclude_none=True), run)

    @app.get("/v1/items/{key}")
    def item(key: str, request: Request) -> Any:
        def run(conn, member):
            found = retrieval.item(conn, key)
            if found is None:
                raise auth.Refused(404, f"no item with key or citation key {key!r}")
            return {**found, "collections": member_collections.for_item(
                conn, member["id"], found["key"])}
        return serve(request, "item", {"key": key}, run)

    @app.post("/v1/facets")
    def facets(body: FacetsBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(body.count, limit("per_request"), "values")
            return retrieval.facets(conn, body.field, _filters(body.filters, conn, member),
                                    body.contains, body.count)
        return serve(request, "facets", body.model_dump(exclude_none=True), run)

    @app.post("/v1/bibtex")
    def bibtex(body: BibtexBody, request: Request) -> Any:
        def run(conn, member):
            auth.check_limit(len(body.keys), limit("per_request"), "entries")
            return retrieval.bibtex(conn, body.keys, body.format, body.abstracts)
        return serve(request, "bibtex", body.model_dump(), run)

    # -- files ---------------------------------------------------------------------
    # A PDF never enters the usage log: uploads and downloads are logged by
    # identifier, size and hash, and the list of held PDFs by its length.

    @app.post("/v1/files")
    async def pdf_upload(request: Request,
                     identifier: str | None = Query(None, description="The PDF's DOI or arXiv ID."),
                     item: str | None = Query(None, description="Or the key of the item it is for.")
                     ) -> Any:
        """Receive a member's PDF (the request body, application/pdf). Replies at once;
        the PDF is processed in the background (upload progress)."""
        # Nothing is read before the key is known and the declared size is
        # within the limit; a body without a declared size is read only up
        # to the limit.
        inputs: dict = {"identifier": identifier, "item": item}
        upload_bytes = limit("upload_megabytes") * 1024 * 1024
        declared = request.headers.get("content-length", "")
        too_big = declared.isdigit() and int(declared) > upload_bytes
        accepted = switched_on("files.pdf_upload")
        body = b""
        if accepted and not too_big and await run_in_threadpool(known_key, request):
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
            if not accepted:
                raise auth.Refused(403, "this server does not accept donated PDFs at present")
            if too_big:
                raise auth.Refused(413, f"the PDF is larger than {upload_bytes // 1048576} MB,"
                                        " the largest a member may donate")
            try:
                return files.receive(conn, body, member["id"], data / "holding", identifier, item)
            except ValueError as exc:
                raise auth.Refused(400, str(exc)) from exc
        return await run_in_threadpool(serve, request, "files/pdf_upload", inputs, run)

    def known_key(request: Request) -> bool:
        with connect() as conn:
            try:
                auth.identify(conn, request, login)
                conn.commit()
            except auth.Refused:
                return False
            return True

    @app.get("/v1/files/held")
    def held(request: Request) -> Any:
        """Every item with a held PDF (key, DOI, arXiv ID, MD5 of each PDF as
        uploaded), for comparing locally before uploading."""
        def run(conn, member):
            reply = files.held(conn)
            response = JSONResponse(reply)
            response.logged = {"count": reply["count"]}
            return response
        return serve(request, "files/held", {}, run)

    @app.get("/v1/pdf_uploads")
    def pdf_uploads(request: Request, count: int = Query(50, ge=1), offset: int = Query(0, ge=0)) -> Any:
        """The member's own PDF uploads, newest first, with their state."""
        def run(conn, member):
            auth.check_limit(count, limit("per_request"), "pdf_uploads")
            return files.pdf_uploads(conn, member["id"], count, offset)
        return serve(request, "pdf_uploads", {"count": count, "offset": offset}, run)

    @app.get("/v1/files/{key}")
    def download(key: str, request: Request) -> Any:
        """The held PDF of an item (key or citation key)."""
        def run(conn, member):
            if not switched_on("files.pdf_download"):
                raise auth.Refused(403, "this server does not hand out PDFs at present; the"
                                        " full texts it holds stay searchable")
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

    # -- collections ---------------------------------------------------------------
    # A member's own lists of items; no member sees another's.

    def own(request: Request, name: str, inputs: dict, work) -> Any:
        def run(conn, member):
            try:
                return work(conn, member["id"])
            except member_collections.NotFound as exc:
                raise auth.Refused(404, str(exc)) from exc
            except member_collections.Invalid as exc:
                raise auth.Refused(400, str(exc)) from exc
        return serve(request, f"collections/{name}", inputs, run)

    @app.post("/v1/collections/list")
    def collections_list(body: CollectionsListBody, request: Request) -> Any:
        def work(conn, member):
            auth.check_limit(body.count, limit("per_request"), "collections")
            return member_collections.listing(conn, member, **body.model_dump())
        return own(request, "list", body.model_dump(exclude_none=True), work)

    @app.post("/v1/collections/show")
    def collection_show(body: CollectionShowBody, request: Request) -> Any:
        """One collection and a page of its items, in the collection's order."""
        def work(conn, member):
            auth.check_limit(body.count, limit("per_request"))
            row = member_collections.find(conn, member, body.collection)
            page = member_collections.ordered_keys(conn, row["id"], body.count, body.offset)
            fields = list(body.fields or retrieval.DEFAULT_FIELDS)
            with_key = fields if "key" in fields else ["key", *fields]
            got = retrieval.list_items(conn, {"item_keys": [p["item"] for p in page]}, len(page),
                                       0, "added", with_key)["items"] if page else []
            by_key = {item["key"]: item for item in got}
            items = []
            for p in page:
                item = dict(by_key[p["item"]])
                if "key" not in fields:
                    del item["key"]
                items.append({**item, **({"rank": p["rank"]} if p["rank"] is not None else {})})
            return {**member_collections.shown(row, member_collections.size(conn, row["id"])),
                    "items": items}
        return own(request, "show", body.model_dump(exclude_none=True), work)

    @app.post("/v1/collections/add")
    def collection_add(body: CollectionAddBody, request: Request) -> Any:
        def work(conn, member):
            auth.check_limit(len(body.keys), limit("per_request"), "items")
            return member_collections.add(conn, member, body.collection, body.keys,
                                          body.from_collection)
        return own(request, "add", body.model_dump(exclude_none=True), work)

    @app.post("/v1/collections/record")
    def collection_record(body: CollectionRecordBody, request: Request) -> Any:
        def work(conn, member):
            auth.check_limit(len(body.keys), limit("per_request"), "items")
            return member_collections.record(conn, member, body.workflow, body.name, body.request,
                                             body.keys, body.collection)
        return own(request, "record", body.model_dump(exclude_none=True), work)

    @app.post("/v1/collections/remove")
    def collection_remove(body: CollectionRemoveBody, request: Request) -> Any:
        def work(conn, member):
            auth.check_limit(len(body.keys), limit("per_request"), "items")
            return member_collections.remove(conn, member, body.collection, body.keys)
        return own(request, "remove", body.model_dump(), work)

    @app.post("/v1/collections/edit")
    def collection_edit(body: CollectionEditBody, request: Request) -> Any:
        return own(request, "edit", body.model_dump(exclude_none=True),
                   lambda conn, member: member_collections.edit(
                       conn, member, body.collection, body.name, body.keep, body.delete))

    # -- the admin menu ------------------------------------------------------------
    # For admins only (403 otherwise). A request that changes something
    # answers with a preview until it is sent with confirm (adminmenu).

    def admin(request: Request, name: str, inputs: dict, work) -> Any:
        def run(conn, member):
            auth.require_admin(member)
            try:
                return work(conn, member)
            except adminmenu.Invalid as exc:
                raise auth.Refused(400, str(exc)) from exc
        return serve(request, f"admin/{name}", inputs, run)

    @app.get("/v1/admin/overview")
    def admin_overview(request: Request) -> Any:
        """The state of the server on one page."""
        return admin(request, "overview", {}, lambda conn, member: adminmenu.overview(
            conn, live(), getattr(embedder, "device", "")))

    @app.post("/v1/admin/settings")
    def admin_settings(body: AdminSettingsBody, request: Request) -> Any:
        return admin(request, "settings", body.model_dump(exclude_none=True),
                     lambda conn, member: adminmenu.settings_request(
                         conn, member, live(), body.name, body.value, body.confirm))

    @app.post("/v1/admin/members")
    def admin_members(body: AdminMembersBody, request: Request) -> Any:
        def work(conn, member):
            reply = adminmenu.members_request(conn, member, body.action, body.name, body.confirm)
            if "api_key" not in reply:
                return reply
            response = JSONResponse(reply)  # a new key is shown once and never logged
            response.logged = {k: v for k, v in reply.items() if k != "api_key"}
            return response
        return admin(request, "members", body.model_dump(exclude_none=True), work)

    @app.post("/v1/admin/venues")
    def admin_venues(body: AdminVenuesBody, request: Request) -> Any:
        def work(conn, member):
            auth.check_limit(body.count, limit("per_request"), "venues")
            return adminmenu.venues_request(conn, member, live(), **body.model_dump())
        return admin(request, "venues", body.model_dump(exclude_none=True), work)

    @app.post("/v1/admin/runs")
    def admin_runs(body: AdminRunsBody, request: Request) -> Any:
        return admin(request, "runs", body.model_dump(exclude_none=True),
                     lambda conn, member: adminmenu.runs_request(
                         conn, member, live(), **body.model_dump()))

    @app.post("/v1/admin/jobs")
    def admin_jobs(body: AdminJobsBody, request: Request) -> Any:
        def work(conn, member):
            auth.check_limit(body.count, limit("per_request"), "jobs")
            return adminmenu.jobs_request(conn, member, **body.model_dump())
        return admin(request, "jobs", body.model_dump(exclude_none=True), work)

    @app.exception_handler(Exception)
    def unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": "the server could not handle this request; its"
                                      " administrator can find the cause in the usage log"},
                            status_code=500)

    return app
