"""Logging the member in at the server's login provider (OpenID Connect).

The client is a public client: it holds no secret. A login is the
authorization code flow with PKCE. The client makes a random value for
this one login (the code verifier) and sends the provider only its SHA-256
hash (the code challenge); the code the provider returns can be exchanged
for tokens only together with the verifier, which never leaves this
process.

Two ways to log in:

    browser_login   opens the member's browser at the provider and waits
                    on this computer (http://127.0.0.1:<a free port>/callback)
                    for the provider to send the browser back with the code
    device_login    for a machine without a browser: prints a web address
                    and a short code to enter on any device, then asks the
                    provider until the member has logged in there

Both ask for the scope `openid offline_access`, so the provider returns a
refresh token that outlasts the browser session. The refresh token is kept
in the client's settings file; Session exchanges it for a short-lived
access token whenever the last one is about to expire. Nothing about the
person (name, email address) is asked for or read.
"""

import base64
import hashlib
import http.server
import secrets
import time
import urllib.parse
import webbrowser
from collections.abc import Callable

import httpx

CALLBACK_PATH = "/callback"
WAIT_SECONDS = 300   # how long a browser login waits for the member
EARLY_SECONDS = 30   # an access token is renewed this long before it expires
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class LoginError(Exception):
    """A login that did not succeed; the message is plain and complete."""


class LoginExpired(LoginError):
    """The stored login is no longer accepted: the member logs in again."""


def pkce() -> tuple[str, str]:
    """(code verifier, code challenge) for one login."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class Provider:
    """The provider's endpoints, read from its own description."""

    def __init__(self, issuer: str, client_id: str, scope: str = "openid offline_access",
                 transport: httpx.BaseTransport | None = None):
        self.issuer, self.client_id, self.scope = issuer.rstrip("/"), client_id, scope
        self._http = httpx.Client(timeout=30, transport=transport)
        self._found: dict | None = None

    def endpoint(self, name: str) -> str:
        if self._found is None:
            try:
                reply = self._http.get(self.issuer + "/.well-known/openid-configuration")
                reply.raise_for_status()
                self._found = reply.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise LoginError(f"The login provider at {self.issuer} cannot be reached"
                                 f" ({type(exc).__name__}).") from exc
        if name not in self._found:
            raise LoginError(f"The login provider at {self.issuer} offers no {name}.")
        return self._found[name]

    def post(self, endpoint: str, **form) -> tuple[int, dict]:
        """(status, reply) of one request to the provider."""
        try:
            reply = self._http.post(self.endpoint(endpoint),
                                    data={"client_id": self.client_id, **form})
            return reply.status_code, reply.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LoginError(f"The login provider at {self.issuer} cannot be reached"
                             f" ({type(exc).__name__}).") from exc

    def tokens(self, **form) -> dict:
        """The tokens of one exchange; LoginError when the provider refuses."""
        status, reply = self.post("token_endpoint", **form)
        if status != 200:
            raise LoginError("The login provider refused: "
                             + str(reply.get("error_description") or reply.get("error") or status))
        return reply


# -- in the browser -------------------------------------------------------------

DONE_PAGE = ("<html><body style='font-family:sans-serif;margin:3em'><h2>{title}</h2>"
             "<p>{text}</p></body></html>")


def _listener(result: dict):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — the name http.server calls
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != CALLBACK_PATH:
                self.send_error(404)
                return
            result.update({k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()})
            ok = "code" in result
            page = DONE_PAGE.format(
                title="Logged in to Scriptorium" if ok else "The login did not succeed",
                text="You can close this tab and return to the terminal.").encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):  # nothing is printed per request
            pass

    return http.server.HTTPServer(("127.0.0.1", 0), Handler)


def browser_login(provider: Provider, open_browser: Callable[[str], object] | None = None,
                  show: Callable[[str], None] = print, wait_seconds: float = WAIT_SECONDS) -> dict:
    """Log the member in through their browser; the provider's tokens."""
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(24)
    result: dict = {}
    server = _listener(result)
    try:
        redirect = f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"
        address = provider.endpoint("authorization_endpoint") + "?" + urllib.parse.urlencode({
            "client_id": provider.client_id, "response_type": "code", "scope": provider.scope,
            "redirect_uri": redirect, "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256"})
        show("Opening your browser to log in. If it does not open, visit this address on"
             f" this computer:\n\n  {address}\n")
        (open_browser or webbrowser.open)(address)
        deadline = time.monotonic() + wait_seconds
        while not result and time.monotonic() < deadline:
            server.timeout = max(0.1, min(1.0, deadline - time.monotonic()))
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        raise LoginError("No login arrived within the waiting time. Run the command again; on a"
                         " machine without a browser, add --device.")
    if result.get("state") != state:
        raise LoginError("The reply did not belong to this login; nothing was accepted.")
    if "code" not in result:
        raise LoginError("The login provider reported: "
                         + str(result.get("error_description") or result.get("error")))
    return provider.tokens(grant_type="authorization_code", code=result["code"],
                           redirect_uri=redirect, code_verifier=verifier)


# -- on another device ----------------------------------------------------------


def device_login(provider: Provider, show: Callable[[str], None] = print,
                 sleep: Callable[[float], None] | None = None) -> dict:
    """Log the member in on another device; the provider's tokens."""
    verifier, challenge = pkce()
    status, start = provider.post("device_authorization_endpoint", scope=provider.scope,
                                  code_challenge=challenge, code_challenge_method="S256")
    if status != 200:
        raise LoginError("The login provider does not offer a login on another device here: "
                         + str(start.get("error_description") or start.get("error") or status))
    show("To log in, open this address on any device and confirm the code"
         f" {start['user_code']}:\n\n"
         f"  {start.get('verification_uri_complete') or start['verification_uri']}\n\n"
         "Only do this for a login you started yourself. Waiting ...")
    interval = float(start.get("interval", 5))
    deadline = time.monotonic() + float(start.get("expires_in", 600))
    while time.monotonic() < deadline:
        (sleep or time.sleep)(interval)
        status, reply = provider.post("token_endpoint", grant_type=DEVICE_GRANT,
                                      device_code=start["device_code"], code_verifier=verifier)
        if status == 200:
            return reply
        error = reply.get("error")
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise LoginError("The login provider refused: "
                             + str(reply.get("error_description") or error or status))
    raise LoginError("The code expired before the login was confirmed. Run the command again.")


# -- staying logged in ----------------------------------------------------------


class Session:
    """The member's login: gives a valid access token, renewing it with
    the stored refresh token when needed. `stored()` reads the refresh
    token as last saved (another session of the client may have renewed
    it) and `save(token)` keeps a new one."""

    def __init__(self, provider: Provider, refresh_token: str,
                 save: Callable[[str], None] = lambda token: None,
                 stored: Callable[[], str] = lambda: "",
                 clock: Callable[[], float] = time.monotonic):
        self.provider, self._refresh, self._save, self._stored = provider, refresh_token, save, stored
        self._clock = clock
        self._access, self._expires = "", 0.0

    def accept(self, tokens: dict) -> None:
        """Take the tokens of a login or renewal."""
        self._access = tokens["access_token"]
        self._expires = self._clock() + float(tokens.get("expires_in", 60))
        newer = tokens.get("refresh_token")
        if newer and newer != self._refresh:
            self._refresh = newer
            self._save(newer)

    def renew(self) -> str:
        self._refresh = self._stored() or self._refresh
        status, reply = self.provider.post("token_endpoint", grant_type="refresh_token",
                                           refresh_token=self._refresh)
        if status != 200:
            if reply.get("error") == "invalid_grant":
                raise LoginExpired("Your Scriptorium login has expired or was withdrawn. Run"
                                   " `scriptorium-client login` in a terminal to log in again.")
            raise LoginError("The login provider refused to renew the login: "
                             + str(reply.get("error_description") or reply.get("error") or status))
        self.accept(reply)
        return self._access

    def token(self) -> str:
        """An access token good for at least the next half minute."""
        if self._access and self._clock() < self._expires - EARLY_SECONDS:
            return self._access
        return self.renew()
