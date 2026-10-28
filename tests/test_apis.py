"""Shared web-API helpers: retries, 404 handling, and the S2 key's reach."""

import pytest
import requests

from scriptorium import apis


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(apis.time, "sleep", lambda _: None)


def test_rate_limits_and_errors_are_retried(monkeypatch):
    monkeypatch.setattr(apis, "s2_headers", dict)
    s = _Session([_Resp(429), requests.ConnectionError("x"), _Resp(200, {"ok": 1})])
    assert apis.request_json(s, "GET", "https://api.crossref.org/works") == {"ok": 1}
    assert len(s.calls) == 3


def test_last_error_is_raised(monkeypatch):
    s = _Session([requests.ConnectionError("a"), requests.ConnectionError("b")])
    with pytest.raises(requests.ConnectionError):
        apis.request_json(s, "GET", "https://api.crossref.org/x", attempts=2)


def test_404_returns_not_found_only_when_given():
    assert apis.request_json(_Session([_Resp(404)]), "GET", "https://api.openalex.org/w",
                             not_found={}) == {}
    with pytest.raises(requests.HTTPError):
        apis.request_json(_Session([_Resp(404)]), "GET", "https://api.openalex.org/w",
                          attempts=1)


def test_client_errors_raise_at_once_and_server_errors_are_retried():
    s = _Session([_Resp(404), _Resp(200)])
    with pytest.raises(requests.HTTPError):
        apis.request_json(s, "GET", "https://api.crossref.org/works/x")
    assert len(s.calls) == 1
    s = _Session([_Resp(408), _Resp(500), _Resp(200, {"ok": 1})])
    assert apis.request_json(s, "GET", "https://api.crossref.org/works/x") == {"ok": 1}
    s = _Session([_Resp(500)] * 4)
    with pytest.raises(requests.HTTPError):
        apis.request_json(s, "GET", "https://api.crossref.org/works/x")
    assert len(s.calls) == 4


def test_s2_key_goes_to_s2_only_and_without_redirects(monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "secret")
    s = _Session([_Resp(200), _Resp(200)])
    apis.request_json(s, "GET", f"{apis.S2}/paper/x")
    apis.request_json(s, "GET", "https://api.crossref.org/works")
    (_, _, s2), (_, _, cr) = s.calls
    assert s2["headers"] == {"x-api-key": "secret"} and s2["allow_redirects"] is False
    assert cr["headers"] == {} and cr["allow_redirects"] is True


def test_crossref_get_adds_mailto_and_unwraps_message(monkeypatch):
    monkeypatch.setattr(apis, "contact_email", lambda: "me@example.org")
    s = _Session([_Resp(200, {"message": {"items": []}})])
    assert apis.crossref_get(s, f"{apis.CROSSREF}/works", rows=1) == {"items": []}
    assert s.calls[0][2]["params"] == {"rows": 1, "mailto": "me@example.org"}


def test_no_contact_email_sends_no_mailto(monkeypatch):
    monkeypatch.setattr(apis, "contact_email", lambda: "")
    s = _Session([_Resp(200, {"message": {}})])
    apis.crossref_get(s, f"{apis.CROSSREF}/works", rows=1)
    assert s.calls[0][2]["params"] == {"rows": 1}
    assert apis.http_session("x").headers["User-Agent"] == "scriptorium-x"


def test_norm_doi_and_strip_jats():
    assert apis.norm_doi(" https://doi.org/10.1/ABC ") == "10.1/abc"
    assert apis.strip_jats("<jats:p>Two  <i>words</i></jats:p>") == "Two words"
