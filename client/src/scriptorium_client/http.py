"""The server's requests, one function each (Table 1 of the README).

Every request carries the member's API key, this client's version and the
name of the agent using it. What can go wrong between client and server is
turned into one plain sentence the agent can pass on: an old client, a key
the server does not accept, a limit, a server that cannot be reached.
"""

import httpx

from scriptorium_client import __version__


class ServerError(Exception):
    """A request the server did not serve; the message is plain and complete."""


class Server:
    def __init__(self, settings: dict, agent: str = "", transport: httpx.BaseTransport | None = None):
        self.address = str(settings.get("server", "")).rstrip("/")
        self.agent = agent
        self._http = httpx.Client(
            base_url=self.address, timeout=120, transport=transport,
            headers={"Authorization": f"Bearer {settings.get('api_key', '')}",
                     "X-Scriptorium-Client": __version__})

    def _send(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        if not self.address:
            raise ServerError("No server is set up yet. Run `scriptorium-client setup` in a"
                              " terminal to enter the server's address and your API key.")
        headers = {"X-Scriptorium-Agent": self.agent} if self.agent else {}
        try:
            reply = self._http.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ServerError(
                f"The Scriptorium server at {self.address} cannot be reached"
                f" ({type(exc).__name__}). Off campus, connect the university VPN first.") from exc
        if reply.status_code < 400:
            return reply
        try:
            detail = reply.json()
            message = detail.get("error") or str(detail.get("detail") or detail)
        except ValueError:
            message = reply.text[:300]
        if reply.status_code == 401:
            raise ServerError("The server does not accept this API key. Run `scriptorium-client"
                              " setup` in a terminal to enter a valid one.")
        if reply.status_code == 426:
            raise ServerError(f"This Scriptorium client is too old for the server: {message}."
                              " Update the client, then restart the agent.")
        raise ServerError(f"The server refused the request ({reply.status_code}): {message}")

    def _json(self, method: str, path: str, body: dict | None = None) -> dict:
        return self._send(method, path, body).json()

    # -- one function per request ------------------------------------------------

    def health(self) -> dict:
        return self._json("GET", "/v1/health")

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


def _clean(body: dict) -> dict:
    """The body without inputs left empty."""
    return {k: v for k, v in body.items() if v is not None and v != {} and v != []}
