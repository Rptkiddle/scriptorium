"""The server's requests, one function each (Table 1 of the README).

Every request carries the member's login (an access token of the server's
login provider, renewed when needed) or, where the member has none, their
API key; this client's version; and the name of the agent using it. What can go wrong between client and server is
turned into one plain sentence the agent can pass on: an old client, a key
the server does not accept, a limit, a server that cannot be reached.
"""

import httpx

from scriptorium_client import __version__
from scriptorium_client import settings as client_settings
from scriptorium_client.login import LoginError, Provider, Session


class ServerError(Exception):
    """A request the server did not serve; the message is plain and complete.
    `status` is the server's HTTP status, when it answered."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimited(ServerError):
    """The server's rate per member was reached; trying again later works."""


class Server:
    def __init__(self, settings: dict, agent: str = "", transport: httpx.BaseTransport | None = None,
                 session: Session | None = None):
        self.address = str(settings.get("server", "")).rstrip("/")
        self.agent = agent
        self._key = str(settings.get("api_key", ""))
        self.session = session or stored_session(settings)
        self._http = httpx.Client(base_url=self.address, timeout=120, transport=transport,
                                  headers={"X-Scriptorium-Client": __version__})

    def _bearer(self, renew: bool = False) -> str:
        """What identifies the member: the login's access token, else the key."""
        if self.session is None:
            return self._key
        try:
            return self.session.renew() if renew else self.session.token()
        except LoginError as exc:
            raise ServerError(str(exc)) from exc

    def _send(self, method: str, path: str, body: dict | None = None, *,
              content: bytes | None = None, params: dict | None = None,
              headers: dict | None = None) -> httpx.Response:
        if not self.address:
            raise ServerError("No server is set up yet. Run `scriptorium-client setup` in a"
                              " terminal to enter the server's address and log in.")
        headers = {**(headers or {}), **({"X-Scriptorium-Agent": self.agent} if self.agent else {})}
        if content is not None:
            headers["Content-Type"] = "application/pdf"

        def once(renew: bool = False) -> httpx.Response:
            headers["Authorization"] = f"Bearer {self._bearer(renew)}"
            try:
                return self._http.request(method, path, json=body, content=content,
                                          params={k: v for k, v in (params or {}).items()
                                                  if v is not None}, headers=headers)
            except httpx.HTTPError as exc:
                raise ServerError(
                    f"The Scriptorium server at {self.address} cannot be reached"
                    f" ({type(exc).__name__}). Off campus, connect the university VPN first.") from exc

        reply = once()
        if reply.status_code == 401 and self.session is not None:
            reply = once(renew=True)  # the token may have just expired: renew it and ask again
        if reply.status_code < 400:
            return reply
        try:
            detail = reply.json()
            message = detail.get("error") or str(detail.get("detail") or detail)
        except ValueError:
            message = reply.text[:300]
        if reply.status_code == 401 and self.session is not None:
            raise ServerError(f"The server does not accept this login ({message}). Run"
                              " `scriptorium-client login` in a terminal to log in again.")
        if reply.status_code == 401:
            raise ServerError(f"The server does not accept this API key ({message}). Run"
                              " `scriptorium-client setup` in a terminal.")
        if reply.status_code == 429:
            raise RateLimited(f"The server refused the request (429): {message}", 429)
        if reply.status_code == 426:
            raise ServerError(f"This Scriptorium client is too old for the server: {message}."
                              " Update the client, then restart the agent.")
        raise ServerError(f"The server refused the request ({reply.status_code}): {message}",
                          reply.status_code)

    def _json(self, method: str, path: str, body: dict | None = None) -> dict:
        return self._send(method, path, body).json()

    # -- one function per request ------------------------------------------------

    def health(self, carry_key: bool = False) -> dict:
        """`carry_key` sends the member's API key beside their login, once,
        so the server attaches the login to the member the key belongs to."""
        headers = {"X-Scriptorium-Key": self._key} if carry_key and self._key else None
        return self._send("GET", "/v1/health", headers=headers).json()

    def login_details(self) -> dict:
        """Where members of this server log in ({"issuer": ""} when it uses
        keys only, as servers from before the login do). Asked without a
        key or login."""
        try:
            reply = self._http.get("/v1/login")
        except httpx.HTTPError as exc:
            raise ServerError(f"The Scriptorium server at {self.address} cannot be reached"
                              f" ({type(exc).__name__}).") from exc
        if reply.status_code == 404:
            return {"issuer": "", "client_id": "", "keys_accepted": True}
        if reply.status_code >= 400:
            raise ServerError(f"The server refused the request ({reply.status_code}).")
        return reply.json()

    def lookup(self, inputs: list[str]) -> dict:
        return self._json("POST", "/v1/lookup", {"inputs": inputs})

    def items(self, **body) -> dict:
        return self._json("POST", "/v1/items", _clean(body))

    def search_items(self, **body) -> dict:
        return self._json("POST", "/v1/search/items", _clean(body))

    def search_passages(self, **body) -> dict:
        return self._json("POST", "/v1/search/passages", _clean(body))

    def item(self, key: str) -> dict:
        return self._json("GET", f"/v1/items/{key}")

    def facets(self, **body) -> dict:
        return self._json("POST", "/v1/facets", _clean(body))

    def bibtex(self, keys: list[str], format: str = "biblatex", abstracts: bool = False) -> dict:
        return self._json("POST", "/v1/bibtex", {"keys": keys, "format": format,
                                                 "abstracts": abstracts})


    # -- files ---------------------------------------------------------------------

    def pdf_upload(self, data: bytes, identifier: str | None = None, item: str | None = None) -> dict:
        return self._send("POST", "/v1/files", content=data,
                          params={"identifier": identifier, "item": item}).json()

    def held(self) -> dict:
        return self._json("GET", "/v1/files/held")

    def pdf_uploads(self, count: int = 50, offset: int = 0) -> dict:
        return self._send("GET", "/v1/pdf_uploads", params={"count": count, "offset": offset}).json()

    def download(self, key: str) -> tuple[bytes, str, str]:
        """(the PDF's bytes, its file name, its SHA-256 as the server holds it)."""
        reply = self._send("GET", f"/v1/files/{key}")
        disposition = reply.headers.get("content-disposition", "")
        name = disposition.split("filename=", 1)[-1].strip('"; ') if "filename=" in disposition else f"{key}.pdf"
        return reply.content, name, reply.headers.get("x-scriptorium-sha256", "")


    # -- collections (the member's own) --------------------------------------------

    def collections(self, action: str, /, **body) -> dict:
        """One collections request: list, show, add, record, remove or edit."""
        return self._json("POST", f"/v1/collections/{action}", _clean(body))

    # -- the admin menu (the server refuses these to members who are not admins) ----

    def admin_overview(self) -> dict:
        return self._json("GET", "/v1/admin/overview")

    def admin(self, menu: str, **body) -> dict:
        """One request of the admin menu: settings, members, venues, runs or jobs."""
        return self._json("POST", f"/v1/admin/{menu}", _clean(body))


def stored_session(settings: dict, transport: httpx.BaseTransport | None = None) -> Session | None:
    """The member's login as the settings hold it; None when they hold none.
    A renewed refresh token is written back to the settings file; where
    the file cannot be written (an agent app that keeps the client from
    its files) the one given stays in use."""
    if not (settings.get("refresh_token") and settings.get("issuer") and settings.get("client_id")):
        return None

    def save(token: str) -> None:
        try:
            client_settings.update({"refresh_token": token})
        except OSError:
            pass

    def stored() -> str:
        return client_settings.load().get("refresh_token", "")

    provider = Provider(settings["issuer"], settings["client_id"], transport=transport)
    return Session(provider, settings["refresh_token"], save=save, stored=stored)


def _clean(body: dict) -> dict:
    """The body without inputs left empty."""
    return {k: v for k, v in body.items() if v is not None and v != {} and v != []}
