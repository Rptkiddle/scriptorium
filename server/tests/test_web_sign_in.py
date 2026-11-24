"""Signing in to the web page: the round trip through the OIDC provider
(a fake one), the member made at the first sign-in, the session cookie and
the header it needs, and signing out."""

from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from scriptorium_server import db as database, members, settings
from scriptorium_server.api import create_app
from scriptorium_server.embedding import FakeEmbedder
from scriptorium_server.oidc import SignInFailed

WEB = {"X-Scriptorium-Web": "1"}
ISSUER = "https://login.example.org/realms/lab"


class FakeProvider:
    """Hands out the login address and, for a known code, the claims."""

    def __init__(self):
        self.people = {"code-ada": {"sub": "sub-ada", "name": "Ada Lovelace",
                                    "preferred_username": "ada"}}
        self.nonces = {}

    def login_url(self, state, nonce, challenge):
        self.nonces[state] = nonce
        return f"https://login.example.org/auth?state={state}"

    def exchange(self, code, verifier, nonce):
        if code not in self.people:
            raise SignInFailed("the sign-in provider refused the code: Code not valid")
        return self.people[code]


@pytest.fixture
def world(db, empty_schema, tmp_path):
    path = tmp_path / "s.toml"
    settings.set_value("web.address", "http://testserver", path)
    settings.set_value("login.issuer", ISSUER, path)
    fake = FakeProvider()
    app = create_app(lambda: database.connect(*empty_schema), FakeEmbedder(),
                     settings.load(path), provider=fake)
    return type("World", (), {"app": app, "db": db, "fake": fake,
                              "browser": TestClient(app, follow_redirects=False)})


def sign_in(browser, code="code-ada"):
    """Go through the round trip; returns the callback's reply."""
    start = browser.get("/auth/login")
    assert start.status_code == 303
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return browser.get("/auth/callback", params={"code": code, "state": state})


def error_of(reply):
    location = reply.headers["location"]
    assert location.startswith("/#signin-error=")
    return unquote(location.removeprefix("/#signin-error="))


def test_signing_in_makes_a_member_and_a_session(world):
    reply = sign_in(world.browser)
    assert reply.status_code == 303 and reply.headers["location"] == "/"
    assert world.browser.get("/auth/me", headers=WEB).json() == {"name": "Ada Lovelace",
                                                                  "admin": False}
    contents = world.browser.get("/v1/contents", headers=WEB)
    assert contents.status_code == 200 and contents.json() == {"items": 0, "venues": 0}
    world.db.rollback()
    log = world.db.execute("SELECT m.name, m.issuer, m.subject, l.request FROM usage_log l"
                           " JOIN members m ON m.id = l.member").fetchall()
    assert [dict(r) for r in log] == [{"name": None, "issuer": ISSUER, "subject": "sub-ada",
                                       "request": "contents"}]   # no name stored


def test_a_second_sign_in_finds_the_same_member(world):
    sign_in(world.browser)
    sign_in(TestClient(world.app, follow_redirects=False))
    world.db.rollback()
    rows = world.db.execute("SELECT subject FROM members").fetchall()
    assert [r["subject"] for r in rows] == ["sub-ada"]


def test_the_page_and_the_client_lead_to_one_member(world):
    client_member = members.create_login(world.db, ISSUER, "sub-ada")   # logged in with the client
    world.db.commit()
    sign_in(world.browser)
    world.db.rollback()
    assert world.db.execute("SELECT count(*) AS n FROM members").fetchone()["n"] == 1
    world.browser.get("/v1/contents", headers=WEB)
    world.db.rollback()
    log = world.db.execute("SELECT member FROM usage_log").fetchall()
    assert [r["member"] for r in log] == [client_member["id"]]


def test_a_sign_in_never_takes_over_a_member_of_the_same_name(world):
    members.add(world.db, "Ada Lovelace")
    world.db.commit()
    sign_in(world.browser)
    world.db.rollback()
    rows = world.db.execute("SELECT name, subject FROM members ORDER BY id").fetchall()
    assert [(r["name"], r["subject"]) for r in rows] == [("Ada Lovelace", None), (None, "sub-ada")]


def test_the_session_counts_only_with_the_web_header(world):
    sign_in(world.browser)
    assert world.browser.get("/v1/contents").status_code == 401     # a page on another site
    assert world.browser.get("/auth/me").status_code == 401
    assert world.browser.post("/auth/logout").status_code == 403
    assert world.browser.get("/v1/contents", headers=WEB).status_code == 200


def test_signing_out_ends_the_session(world):
    sign_in(world.browser)
    assert world.browser.post("/auth/logout", headers=WEB).status_code == 204
    assert world.browser.get("/v1/contents", headers=WEB).status_code == 401


def test_a_revoked_member_cannot_sign_in_and_loses_the_session(world):
    sign_in(world.browser)
    members.revoke(world.db, "sub-ada")
    world.db.commit()
    assert world.browser.get("/v1/contents", headers=WEB).status_code == 401
    assert "revoked" in error_of(sign_in(TestClient(world.app, follow_redirects=False)))


def test_a_callback_not_started_in_this_browser_is_refused(world):
    start = world.browser.get("/auth/login")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    other = TestClient(world.app, follow_redirects=False)
    reply = other.get("/auth/callback", params={"code": "code-ada", "state": state})
    assert "not started in this browser" in error_of(reply)
    assert other.get("/auth/me", headers=WEB).status_code == 401


def test_a_state_is_used_once(world):
    start = world.browser.get("/auth/login")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    world.browser.get("/auth/callback", params={"code": "code-ada", "state": state})
    world.browser.cookies.set("scriptorium_login", state, path="/auth")
    again = world.browser.get("/auth/callback", params={"code": "code-ada", "state": state})
    assert "took too long" in error_of(again)


def test_a_refused_code_or_provider_error_is_shown_to_the_person(world):
    assert "Code not valid" in error_of(sign_in(world.browser, code="wrong"))
    reply = world.browser.get("/auth/callback", params={"error": "access_denied",
                                                       "error_description": "User cancelled"})
    assert "User cancelled" in error_of(reply)


def test_without_a_login_issuer_the_page_says_sign_in_is_not_set_up(db, empty_schema, tmp_path):
    app = create_app(lambda: database.connect(*empty_schema), FakeEmbedder(),
                     settings.load(tmp_path / "none.toml"), provider=FakeProvider())
    reply = TestClient(app, follow_redirects=False).get("/auth/login")
    assert "not set up" in error_of(reply)


def test_without_a_provider_the_page_says_sign_in_is_not_set_up(db, empty_schema, tmp_path):
    app = create_app(lambda: database.connect(*empty_schema), FakeEmbedder(),
                     settings.load(tmp_path / "none.toml"))
    reply = TestClient(app, follow_redirects=False).get("/auth/login")
    assert "not set up" in error_of(reply)
