"""Logging in: the browser login and the login on another device against a
stand-in provider, staying logged in, and what the client sends the server."""

import base64
import hashlib
import json
import stat
import threading
import urllib.parse
import urllib.request

import httpx
import pytest
from typer.testing import CliRunner

from scriptorium_client import cli, login, settings
from scriptorium_client.http import Server, ServerError, stored_session

ISSUER = "https://auth.test/realms/lab"


@pytest.fixture(autouse=True)
def own_settings_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIPTORIUM_CLIENT_SETTINGS", str(tmp_path / "client.toml"))
    for name in settings.DEFAULTS:
        monkeypatch.delenv(settings.variable(name), raising=False)


class FakeProvider:
    """A provider that keeps what it was sent and checks PKCE as a real one does."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.challenge = ""
        self.pending = 0          # how often a device login is still "pending"
        self.refresh_ok = True
        self.rotate = False
        self.issued = 0

    def tokens(self) -> dict:
        self.issued += 1
        return {"access_token": f"access-{self.issued}", "expires_in": 300,
                "refresh_token": f"refresh-{self.issued}" if self.rotate else "refresh-1"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "authorization_endpoint": ISSUER + "/auth", "token_endpoint": ISSUER + "/token",
                "device_authorization_endpoint": ISSUER + "/device"})
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.requests.append((path.rsplit("/", 1)[-1], form))
        if path.endswith("/device"):
            self.challenge = form.get("code_challenge", "")
            return httpx.Response(200, json={
                "device_code": "dev-1", "user_code": "ABCD-EFGH", "interval": 5, "expires_in": 600,
                "verification_uri": ISSUER + "/device",
                "verification_uri_complete": ISSUER + "/device?user_code=ABCD-EFGH"})
        grant = form.get("grant_type")
        if grant == "refresh_token":
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self.tokens())
        if grant == login.DEVICE_GRANT and self.pending:
            self.pending -= 1
            return httpx.Response(400, json={"error": "authorization_pending"})
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != self.challenge:
            return httpx.Response(400, json={"error": "invalid_grant",
                                             "error_description": "PKCE verification failed"})
        return httpx.Response(200, json=self.tokens())

    def browser(self, change=lambda query: query):
        """What the member's browser does: the provider sends it back to the
        client's address on this computer with a code."""
        def open_browser(address: str) -> None:
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(address).query))
            self.authorization = query
            self.challenge = query["code_challenge"]
            back = change({"code": "code-1", "state": query["state"]})
            target = query["redirect_uri"] + "?" + urllib.parse.urlencode(back)

            def visit():
                try:
                    urllib.request.urlopen(target, timeout=5).read()
                except OSError:
                    pass
            threading.Thread(target=visit, daemon=True).start()
        return open_browser


@pytest.fixture
def fake():
    return FakeProvider()


@pytest.fixture
def provider(fake):
    return login.Provider(ISSUER, "scriptorium-client", transport=httpx.MockTransport(fake))


def quiet(text):
    pass


def test_a_browser_login_sends_a_challenge_and_exchanges_the_code_with_its_verifier(fake, provider):
    tokens = login.browser_login(provider, open_browser=fake.browser(), show=quiet)
    assert tokens["access_token"] == "access-1"
    asked = fake.authorization
    assert asked["client_id"] == "scriptorium-client" and asked["response_type"] == "code"
    assert asked["scope"] == "openid offline_access" and asked["code_challenge_method"] == "S256"
    assert asked["redirect_uri"].startswith("http://127.0.0.1:")
    assert asked["redirect_uri"].endswith("/callback")
    kind, form = fake.requests[-1]
    assert (kind, form["grant_type"], form["code"]) == ("token", "authorization_code", "code-1")
    assert form["redirect_uri"] == asked["redirect_uri"] and "client_secret" not in form


def test_a_reply_that_belongs_to_another_login_is_not_accepted(fake, provider):
    forged = fake.browser(lambda query: {**query, "state": "someone-elses"})
    with pytest.raises(login.LoginError, match="did not belong to this login"):
        login.browser_login(provider, open_browser=forged, show=quiet)
    assert not fake.requests  # the code was never exchanged


def test_a_login_the_provider_reports_as_failed_says_why(fake, provider):
    denied = fake.browser(lambda query: {"state": query["state"], "error": "access_denied"})
    with pytest.raises(login.LoginError, match="access_denied"):
        login.browser_login(provider, open_browser=denied, show=quiet)


def test_a_browser_login_nobody_completes_gives_up(provider):
    with pytest.raises(login.LoginError, match="No login arrived"):
        login.browser_login(provider, open_browser=lambda address: None, show=quiet,
                            wait_seconds=0.3)


def test_a_login_on_another_device_shows_the_code_and_asks_until_confirmed(fake, provider):
    fake.pending, shown, waits = 2, [], []
    tokens = login.device_login(provider, show=shown.append, sleep=waits.append)
    assert tokens["access_token"] == "access-1"
    assert "ABCD-EFGH" in shown[0] and ISSUER + "/device?user_code=ABCD-EFGH" in shown[0]
    assert waits == [5.0, 5.0, 5.0]
    start = fake.requests[0][1]
    assert start["scope"] == "openid offline_access" and start["code_challenge_method"] == "S256"
    assert fake.requests[-1][1]["device_code"] == "dev-1"


def test_a_session_renews_the_access_token_only_when_it_is_about_to_expire(fake, provider):
    now = [0.0]
    session = login.Session(provider, "refresh-1", clock=lambda: now[0])
    assert session.token() == "access-1" and session.token() == "access-1"
    now[0] = 300 - login.EARLY_SECONDS + 1
    assert session.token() == "access-2"
    assert [form["grant_type"] for _, form in fake.requests] == ["refresh_token"] * 2


def test_a_renewed_refresh_token_is_saved_and_one_saved_by_another_session_is_used(fake, provider):
    fake.rotate, saved = True, []
    session = login.Session(provider, "refresh-0", save=saved.append, stored=lambda: "refresh-9")
    session.token()
    assert fake.requests[-1][1]["refresh_token"] == "refresh-9"  # the stored one, not the stale one
    assert saved == ["refresh-1"]


def test_an_expired_login_tells_the_member_to_log_in_again(fake, provider):
    fake.refresh_ok = False
    with pytest.raises(login.LoginExpired, match="scriptorium-client login"):
        login.Session(provider, "refresh-1").token()


# -- what the server is sent ----------------------------------------------------


class FakeScriptorium:
    def __init__(self):
        self.seen: list[dict] = []
        self.refuse_once = False
        self.login = {"issuer": ISSUER, "client_id": "scriptorium-client",
                      "scope": "openid offline_access", "keys_accepted": True}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/login":
            assert "authorization" not in request.headers
            return httpx.Response(200, json=self.login) if self.login else httpx.Response(404)
        self.seen.append(dict(request.headers))
        if self.refuse_once:
            self.refuse_once = False
            return httpx.Response(401, json={"error": "login not accepted: the token has expired"})
        return httpx.Response(200, json={"status": "ok", "server": "0.1.0", "items": 5,
                                         "fulltexts": 1})


def server(fake, scriptorium, **more):
    config = {"server": "https://scriptorium.test", **more}
    session = stored_session(config, transport=httpx.MockTransport(fake))
    return Server(config, transport=httpx.MockTransport(scriptorium), session=session)


LOGGED_IN = {"issuer": ISSUER, "client_id": "scriptorium-client", "refresh_token": "refresh-1"}


def test_a_logged_in_client_sends_the_access_token_not_the_key(fake):
    scriptorium = FakeScriptorium()
    server(fake, scriptorium, api_key="scr_old", **LOGGED_IN).health()
    assert scriptorium.seen[0]["authorization"] == "Bearer access-1"
    assert "x-scriptorium-key" not in scriptorium.seen[0]


def test_the_key_goes_along_only_when_asked(fake):
    scriptorium = FakeScriptorium()
    server(fake, scriptorium, api_key="scr_old", **LOGGED_IN).health(carry_key=True)
    assert scriptorium.seen[0]["x-scriptorium-key"] == "scr_old"


def test_a_client_without_a_login_sends_its_key_as_before(fake):
    scriptorium = FakeScriptorium()
    server(fake, scriptorium, api_key="scr_old").health()
    assert scriptorium.seen[0]["authorization"] == "Bearer scr_old" and not fake.requests


def test_a_token_the_server_refuses_is_renewed_once_and_the_request_sent_again(fake):
    scriptorium = FakeScriptorium()
    scriptorium.refuse_once = True
    assert server(fake, scriptorium, **LOGGED_IN).health()["items"] == 5
    assert [h["authorization"] for h in scriptorium.seen] == ["Bearer access-1", "Bearer access-2"]


def test_an_expired_login_reaches_the_agent_as_one_sentence(fake):
    fake.refresh_ok = False
    with pytest.raises(ServerError, match="Run `scriptorium-client login` in a terminal"):
        server(fake, FakeScriptorium(), **LOGGED_IN).health()


def test_a_server_from_before_the_login_is_read_as_using_keys(fake):
    scriptorium = FakeScriptorium()
    scriptorium.login = None
    assert server(fake, scriptorium).login_details()["issuer"] == ""


# -- the commands ---------------------------------------------------------------


@pytest.fixture
def commands(fake, monkeypatch):
    """`setup` and `login` against the stand-in provider and server."""
    scriptorium = FakeScriptorium()
    real_provider, real_server = login.Provider, cli.Server
    monkeypatch.setattr(login, "Provider", lambda *a, **k: real_provider(
        *a, **k, transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(cli, "Server", lambda *a, **k: real_server(
        *a, **k, transport=httpx.MockTransport(scriptorium)))
    monkeypatch.setattr(login.webbrowser, "open", fake.browser())
    return type("Commands", (), {"scriptorium": scriptorium, "run": CliRunner().invoke})


def test_setup_logs_in_and_saves_the_login_readable_by_the_owner_only(commands, fake):
    settings.save({"server": "https://old.test", "api_key": "scr_old", "reply_chars": 9000})
    result = commands.run(cli.app, ["setup", "--server", "scriptorium.test"])
    assert result.exit_code == 0, result.output
    assert "Logged in." in result.output
    stored = settings.load(environment=False)
    assert stored["server"] == "https://scriptorium.test" and stored["refresh_token"] == "refresh-1"
    assert (stored["issuer"], stored["client_id"]) == (ISSUER, "scriptorium-client")
    assert stored["api_key"] == "scr_old" and stored["reply_chars"] == 9000  # left as they were
    assert commands.scriptorium.seen[0]["x-scriptorium-key"] == "scr_old"  # sent this once
    path = settings.settings_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "results_dir" not in path.read_text()


def test_login_on_another_device(commands, fake, monkeypatch):
    monkeypatch.setattr(login.time, "sleep", lambda seconds: None)
    settings.save({"server": "https://scriptorium.test"})
    result = commands.run(cli.app, ["login", "--device"])
    assert result.exit_code == 0, result.output
    assert "ABCD-EFGH" in result.output and "Logged in." in result.output
    assert settings.load(environment=False)["refresh_token"] == "refresh-1"


def test_login_on_a_server_that_uses_keys_says_so(commands):
    commands.scriptorium.login = None
    settings.save({"server": "https://scriptorium.test"})
    result = commands.run(cli.app, ["login"])
    assert result.exit_code == 1 and "uses API keys" in result.output


def test_setup_on_a_server_that_uses_keys_asks_for_the_key(commands):
    commands.scriptorium.login = None
    result = commands.run(cli.app, ["setup", "--server", "https://scriptorium.test"],
                          input="scr_new\n")
    assert result.exit_code == 0, result.output
    assert settings.load(environment=False)["api_key"] == "scr_new"
