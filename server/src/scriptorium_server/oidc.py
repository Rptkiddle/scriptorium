"""Signing in to the web page through the lab's OIDC provider (Keycloak).

The server is a confidential client using the authorization-code flow
with PKCE: the browser is sent to the provider's login page, comes back to
/auth/callback with a code, and the server exchanges the code, with its
client secret, for the person's ID token. The browser never holds a token;
it gets a session cookie (web.py).

The ID token comes straight from the provider's token endpoint over TLS,
so, as OpenID Connect Core 3.1.3.7 allows, its signature is not checked;
its issuer, audience, expiry and nonce are.
"""

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import requests

SCOPE = "openid profile email"


class SignInFailed(Exception):
    """The provider refused the sign-in, or its answer did not check out."""


def pkce_pair() -> tuple[str, str]:
    """(code verifier, its S256 challenge)."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise SignInFailed("the provider's ID token could not be read") from exc


class Provider:
    """One OIDC provider and this server's client registration with it."""

    def __init__(self, issuer: str, client_id: str, client_secret: str, redirect_uri: str,
                 http: requests.Session | None = None, timeout: float = 10):
        self.issuer, self.client_id, self.client_secret = issuer.rstrip("/"), client_id, client_secret
        self.redirect_uri, self.timeout = redirect_uri, timeout
        self.http = http or requests.Session()
        self._config: dict | None = None

    def config(self) -> dict:
        """The provider's discovery document, fetched once."""
        if self._config is None:
            try:
                reply = self.http.get(f"{self.issuer}/.well-known/openid-configuration",
                                      timeout=self.timeout)
                reply.raise_for_status()
                self._config = reply.json()
            except (requests.RequestException, ValueError) as exc:
                raise SignInFailed(f"the sign-in provider at {self.issuer} does not answer") from exc
        return self._config

    def login_url(self, state: str, nonce: str, challenge: str) -> str:
        query = {"response_type": "code", "client_id": self.client_id,
                 "redirect_uri": self.redirect_uri, "scope": SCOPE, "state": state,
                 "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{self.config()['authorization_endpoint']}?{urlencode(query)}"

    def exchange(self, code: str, verifier: str, nonce: str) -> dict:
        """The checked claims of the person who signed in."""
        data = {"grant_type": "authorization_code", "code": code,
                "redirect_uri": self.redirect_uri, "code_verifier": verifier,
                "client_id": self.client_id}
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        try:
            reply = self.http.post(self.config()["token_endpoint"], data=data, auth=auth,
                                   timeout=self.timeout)
            body = reply.json()
        except (requests.RequestException, ValueError) as exc:
            raise SignInFailed("the sign-in provider did not answer the code exchange") from exc
        if reply.status_code != 200 or "id_token" not in body:
            reason = body.get("error_description") or body.get("error") or reply.status_code
            raise SignInFailed(f"the sign-in provider refused the code: {reason}")
        claims = _claims(body["id_token"])
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if claims.get("iss") != self.config().get("issuer", self.issuer):
            raise SignInFailed("the ID token comes from another issuer")
        if self.client_id not in audiences:
            raise SignInFailed("the ID token is meant for another client")
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < time.time():
            raise SignInFailed("the ID token has expired")
        if not nonce or claims.get("nonce") != nonce:
            raise SignInFailed("the ID token does not belong to this sign-in")
        if not claims.get("sub"):
            raise SignInFailed("the ID token names no person")
        return claims
