"""Login through a provider: which tokens the server accepts, and how a
login becomes a member. A stand-in provider signs the tokens with a key of
its own, so no test reaches a real one."""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from scriptorium_server import db as database, login, members, settings
from scriptorium_server.api import create_app
from scriptorium_server.embedding import FakeEmbedder

ISSUER = "https://auth.test/realms/lab"


class Provider:
    """Signs tokens and serves its public keys, counting how often they are read."""

    def __init__(self, kid="k1"):
        self.kid, self.fetches = kid, 0
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict:
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        return {"keys": [{**key, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def fetch(self, address: str) -> dict:
        self.fetches += 1
        if address.endswith("/.well-known/openid-configuration"):
            return {"jwks_uri": ISSUER + "/certs"}
        return self.jwks()

    def token(self, sub="person-1", **changed) -> str:
        claims = {"iss": ISSUER, "sub": sub, "aud": ["scriptorium-api", "account"],
                  "azp": "scriptorium-client", "exp": int(time.time()) + 300,
                  "name": "Ada Lovelace", "email": "ada@example.org", **changed}
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": self.kid})


@pytest.fixture
def provider():
    return Provider()


@pytest.fixture
def verifier(provider):
    return login.Verifier(ISSUER, "scriptorium-client", "scriptorium-api", fetch=provider.fetch)


def test_a_token_of_the_provider_for_this_server_is_accepted(verifier, provider):
    assert verifier.claims(provider.token())["sub"] == "person-1"


@pytest.mark.parametrize("changed, why", [
    ({"aud": ["another-api"]}, "not meant for this server"),
    ({"azp": "another-client"}, "issued to another application"),
    ({"exp": int(time.time()) - 10}, "expired"),
    ({"iss": "https://auth.test/realms/other"}, "another login provider"),
    ({"sub": None}, "does not pass the check"),
])
def test_a_token_that_fails_one_check_is_refused(verifier, provider, changed, why):
    with pytest.raises(login.Invalid, match=why):
        verifier.claims(provider.token(**changed))


def test_a_token_signed_by_someone_else_is_refused(verifier):
    with pytest.raises(login.Invalid, match="does not pass the check"):
        verifier.claims(Provider().token())  # the same key name, another key


def test_text_that_is_no_token_is_refused(verifier):
    with pytest.raises(login.Invalid, match="not a login token"):
        verifier.claims("not-a-token")


def test_the_keys_are_read_once_and_again_for_a_new_key_at_most_once_a_minute(provider):
    now = [0.0]
    v = login.Verifier(ISSUER, "scriptorium-client", "scriptorium-api", fetch=provider.fetch,
                       clock=lambda: now[0])
    v.claims(provider.token())
    v.claims(provider.token())
    assert provider.fetches == 2  # the provider's description and its keys, once
    newer = Provider(kid="k2")
    with pytest.raises(login.Invalid, match="does not list"):
        v.claims(newer.token())  # within the minute: not read again
    assert provider.fetches == 2
    provider.kid, provider.private = "k2", newer.private  # the provider changed its key
    now[0] = 61.0
    assert v.claims(newer.token())["sub"] == "person-1"
    assert provider.fetches == 4


def test_a_provider_that_cannot_be_reached_refuses_the_token():
    def down(address):
        raise OSError("no route")
    v = login.Verifier(ISSUER, "scriptorium-client", "scriptorium-api", fetch=down)
    with pytest.raises(login.Invalid, match="cannot be read"):
        v.claims(Provider().token())


# -- through the API ------------------------------------------------------------


@pytest.fixture
def world(db, empty_schema, tmp_path, provider):
    path = tmp_path / "s.toml"
    settings.set_value("login.issuer", ISSUER, path)
    key = members.add(db, "ada")
    db.commit()
    app = create_app(lambda: database.connect(*empty_schema), FakeEmbedder(), settings.load(path),
                     current=lambda: settings.load(path), login_fetch=provider.fetch)
    return type("World", (), {"client": TestClient(app), "db": db, "key": key, "path": path,
                              "provider": provider})


def bearer(value, **more):
    return {"Authorization": f"Bearer {value}", **more}


def rows(world):
    world.db.rollback()
    return world.db.execute("SELECT id, name, issuer, subject, key_hash IS NOT NULL AS key,"
                            " revoked_at FROM members ORDER BY id").fetchall()


def test_where_to_log_in_is_served_without_a_key(world):
    assert world.client.get("/v1/login").json() == {
        "issuer": ISSUER, "client_id": "scriptorium-client",
        "scope": "openid offline_access", "keys_accepted": True}


def test_a_server_without_a_provider_says_so_and_takes_no_tokens(world):
    settings.set_value("login.issuer", "", world.path)
    assert world.client.get("/v1/login").json()["issuer"] == ""
    reply = world.client.get("/v1/health", headers=bearer(world.provider.token()))
    assert reply.status_code == 401


def member_of(world, headers):
    """The member a request with these headers is served as (by name)."""
    assert world.client.get("/v1/health", headers=headers).status_code == 200
    world.db.rollback()
    return world.db.execute(
        f"SELECT {members.SHOWN} AS name FROM members WHERE id ="
        " (SELECT member FROM usage_log ORDER BY id DESC LIMIT 1)").fetchone()["name"]


def test_a_first_login_makes_a_member_named_by_the_identifier_and_nothing_about_the_person(world):
    token = world.provider.token("person-7")
    assert member_of(world, bearer(token)) == member_of(world, bearer(token)) == "person-7"
    made = rows(world)[1]
    assert (made["name"], made["issuer"], made["subject"], made["key"]) == (
        None, ISSUER, "person-7", False)
    stored = json.dumps(rows(world), default=str) + json.dumps(
        world.db.execute("SELECT * FROM usage_log").fetchall(), default=str)
    assert "Lovelace" not in stored and "ada@example.org" not in stored


def test_two_people_are_two_members(world):
    assert member_of(world, bearer(world.provider.token("a"))) == "a"
    assert member_of(world, bearer(world.provider.token("b"))) == "b"
    assert len(rows(world)) == 3


def test_a_login_sent_with_a_members_key_is_attached_to_that_member(world):
    token = world.provider.token("person-1")
    assert member_of(world, bearer(token, **{"X-Scriptorium-Key": world.key})) == "ada"
    assert member_of(world, bearer(token)) == "ada"
    assert member_of(world, bearer(world.key)) == "ada"
    assert [(r["name"], r["subject"]) for r in rows(world)] == [("ada", "person-1")]


def test_a_key_already_attached_to_a_login_does_not_take_a_second_one(world):
    headers = {"X-Scriptorium-Key": world.key}
    assert member_of(world, bearer(world.provider.token("person-1"), **headers)) == "ada"
    assert member_of(world, bearer(world.provider.token("person-2"), **headers)) == "person-2"
    assert [r["subject"] for r in rows(world)] == ["person-1", "person-2"]


def test_a_refused_token_makes_no_member(world):
    reply = world.client.get("/v1/health", headers=bearer(world.provider.token(aud=["other"])))
    assert reply.status_code == 401 and "not meant for this server" in reply.json()["error"]
    assert len(rows(world)) == 1


def test_a_revoked_member_is_refused_at_login(world):
    token = world.provider.token("person-7")
    world.client.get("/v1/health", headers=bearer(token))
    assert members.revoke(world.db, "person-7")
    world.db.commit()
    reply = world.client.get("/v1/health", headers=bearer(token))
    assert reply.status_code == 403 and "revoked" in reply.json()["error"]


def test_keys_work_beside_the_login_until_switched_off(world):
    assert world.client.get("/v1/health", headers=bearer(world.key)).status_code == 200
    settings.set_value("login.keys", "off", world.path)
    reply = world.client.get("/v1/health", headers=bearer(world.key))
    assert reply.status_code == 401 and "scriptorium-client login" in reply.json()["error"]
    assert world.client.get("/v1/login").json()["keys_accepted"] is False
    assert world.client.get("/v1/health",
                            headers=bearer(world.provider.token())).status_code == 200


def test_a_member_is_named_by_the_name_given_or_by_the_logins_identifier(world):
    world.client.get("/v1/health", headers=bearer(world.provider.token("person-7")))
    world.client.get("/v1/health", headers=bearer(world.provider.token("person-1"),
                                                  **{"X-Scriptorium-Key": world.key}))
    world.db.rollback()
    assert [r["name"] for r in members.listing(world.db)] == ["ada", "person-7"]
    assert members.find(world.db, "ada")["id"] == 1
    assert members.find(world.db, "person-7")["id"] == 2
    assert members.find(world.db, "person-1") is None  # ada is named ada, not by her login
    assert members.set_admin(world.db, "person-7")
