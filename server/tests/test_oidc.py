"""The OIDC provider client: the login address, the code exchange and the
checks on the ID token. No database, no network: a fake HTTP session."""

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from scriptorium_server.oidc import Provider, SignInFailed, pkce_pair

ISSUER = "https://login.example.org/realms/lab"


def id_token(**claims) -> str:
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    full = {"iss": ISSUER, "aud": "scriptorium-web", "exp": time.time() + 300,
            "nonce": "N", "sub": "user-1", "name": "Ada Lovelace", **claims}
    return f"{part({'alg': 'RS256'})}.{part(full)}.signature"


class Reply:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class FakeHttp:
    def __init__(self, token_reply=None):
        self.token_reply = token_reply or Reply(200, {"id_token": id_token()})
        self.posted = []

    def get(self, url, timeout):
        assert url == f"{ISSUER}/.well-known/openid-configuration"
        return Reply(200, {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth",
                           "token_endpoint": f"{ISSUER}/token"})

    def post(self, url, data, auth, timeout):
        self.posted.append((url, data, auth))
        return self.token_reply


def provider(http):
    return Provider(ISSUER, "scriptorium-web", "s3cret", "https://scr.example.org/auth/callback",
                    http=http)


def test_pkce_challenge_is_the_s256_of_the_verifier():
    verifier, challenge = pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode() and len(verifier) >= 43


def test_login_url_asks_for_a_code_with_pkce():
    url = provider(FakeHttp()).login_url("S", "N", "C")
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{ISSUER}/auth"
    assert query == {"response_type": "code", "client_id": "scriptorium-web",
                     "redirect_uri": "https://scr.example.org/auth/callback",
                     "scope": "openid profile email", "state": "S", "nonce": "N",
                     "code_challenge": "C", "code_challenge_method": "S256"}


def test_exchange_sends_the_code_with_the_client_secret_and_checks_the_token():
    http = FakeHttp()
    claims = provider(http).exchange("CODE", "VERIFIER", "N")
    assert claims["sub"] == "user-1" and claims["name"] == "Ada Lovelace"
    url, data, auth = http.posted[0]
    assert url == f"{ISSUER}/token" and auth == ("scriptorium-web", "s3cret")
    assert data["code"] == "CODE" and data["code_verifier"] == "VERIFIER"
    assert data["grant_type"] == "authorization_code"


@pytest.mark.parametrize("claims, reason", [
    ({"iss": "https://elsewhere.example.org"}, "another issuer"),
    ({"aud": "some-other-client"}, "another client"),
    ({"exp": time.time() - 10}, "expired"),
    ({"nonce": "OTHER"}, "does not belong to this sign-in"),
    ({"sub": ""}, "names no person"),
])
def test_a_token_that_does_not_check_out_is_refused(claims, reason):
    http = FakeHttp(Reply(200, {"id_token": id_token(**claims)}))
    with pytest.raises(SignInFailed, match=reason):
        provider(http).exchange("CODE", "VERIFIER", "N")


def test_an_audience_list_holding_the_client_is_accepted():
    http = FakeHttp(Reply(200, {"id_token": id_token(aud=["account", "scriptorium-web"])}))
    assert provider(http).exchange("CODE", "VERIFIER", "N")["sub"] == "user-1"


def test_a_refused_code_gives_the_providers_reason():
    http = FakeHttp(Reply(400, {"error": "invalid_grant", "error_description": "Code not valid"}))
    with pytest.raises(SignInFailed, match="Code not valid"):
        provider(http).exchange("CODE", "VERIFIER", "N")
