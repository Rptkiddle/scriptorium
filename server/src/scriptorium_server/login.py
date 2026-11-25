"""Login through an OpenID Connect provider: checking the access token a
member's client sends.

The client logs the member in at the provider (authorization code with
PKCE, as a public client) and sends the provider's access token with each
request. The server holds no secret. It checks the token's signature
against the provider's public keys, and that the token is from this
provider (issuer), is not expired, is meant for this server (audience) and
was issued to the Scriptorium client (azp). Every client of a provider
shares its signing keys, so the last two checks are what keep a token
issued for another application from being accepted here.

The token's `sub` identifies the person. Nothing else in the token is
read: no name, no email address.

The provider's public keys are fetched once and kept; a token signed with
a key not yet seen makes the server fetch them again, at most once a
minute.
"""

import time
from collections.abc import Callable

import jwt
import requests

ALGORITHMS = ["RS256"]
REFETCH_SECONDS = 60


class Invalid(Exception):
    """A token the server does not accept; the message says why."""


def fetch_json(address: str) -> dict:
    reply = requests.get(address, timeout=10)
    reply.raise_for_status()
    return reply.json()


class Verifier:
    """Checks tokens of one provider for one client and audience."""

    def __init__(self, issuer: str, client_id: str, audience: str,
                 fetch: Callable[[str], dict] = fetch_json,
                 clock: Callable[[], float] = time.monotonic):
        self.issuer, self.client_id, self.audience = issuer.rstrip("/"), client_id, audience
        self._fetch, self._clock = fetch, clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched: float | None = None

    def _load_keys(self) -> None:
        try:
            found = self._fetch(self.issuer + "/.well-known/openid-configuration")
            keys = self._fetch(found["jwks_uri"])["keys"]
        except Exception as exc:  # noqa: BLE001 — any failure to reach or read the provider
            raise Invalid(f"the login provider's keys cannot be read ({type(exc).__name__})") from exc
        self._fetched = self._clock()
        self._keys = {}
        for key in keys:
            if key.get("use", "sig") == "sig" and key.get("kid"):
                try:
                    self._keys[key["kid"]] = jwt.PyJWK(key)
                except jwt.PyJWTError:
                    continue

    def _key(self, kid: str) -> jwt.PyJWK:
        stale = self._fetched is None or self._clock() - self._fetched >= REFETCH_SECONDS
        if kid not in self._keys and stale:
            self._load_keys()
        if kid not in self._keys:
            raise Invalid("the token is signed with a key the login provider does not list")
        return self._keys[kid]

    def claims(self, token: str) -> dict:
        """The token's contents when it passes every check; Invalid otherwise."""
        try:
            kid = jwt.get_unverified_header(token).get("kid", "")
        except jwt.PyJWTError as exc:
            raise Invalid("this is not a login token") from exc
        key = self._key(kid)
        try:
            claims = jwt.decode(token, key.key, algorithms=ALGORITHMS, issuer=self.issuer,
                                audience=self.audience,
                                options={"require": ["exp", "iss", "sub", "aud"]})
        except jwt.ExpiredSignatureError as exc:
            raise Invalid("the token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise Invalid("the token is not meant for this server") from exc
        except jwt.InvalidIssuerError as exc:
            raise Invalid("the token is from another login provider") from exc
        except jwt.PyJWTError as exc:
            raise Invalid(f"the token does not pass the check ({type(exc).__name__})") from exc
        if claims.get("azp") != self.client_id:
            raise Invalid("the token was issued to another application")
        return claims


class Login:
    """The login settings in force and the verifier that goes with them.
    `current()` gives the settings now, so a changed setting takes effect
    at once; the verifier and its keys are kept while they stay the same."""

    def __init__(self, current: Callable[[], object], fetch: Callable[[str], dict] = fetch_json):
        self._current, self._fetch = current, fetch
        self._verifier: Verifier | None = None

    def _value(self, name: str) -> str:
        return str(self._current()[f"login.{name}"]).strip()

    @property
    def issuer(self) -> str:
        """The provider's address; empty when login is not set up."""
        return self._value("issuer").rstrip("/")

    @property
    def client_id(self) -> str:
        return self._value("client_id")

    @property
    def keys_accepted(self) -> bool:
        """Whether API keys are still accepted (always, without a provider)."""
        return not self.issuer or self._value("keys") == "on"

    def described(self) -> dict:
        """What a client needs to log a member in; public."""
        return {"issuer": self.issuer, "client_id": self.client_id if self.issuer else "",
                "scope": "openid offline_access", "keys_accepted": self.keys_accepted}

    def claims(self, token: str) -> dict:
        wanted = (self.issuer, self.client_id, self._value("audience"))
        v = self._verifier
        if v is None or (v.issuer, v.client_id, v.audience) != wanted:
            v = self._verifier = Verifier(*wanted, fetch=self._fetch)
        return v.claims(token)
