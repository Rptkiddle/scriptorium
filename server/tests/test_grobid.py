"""The GROBID client, against a fake HTTP session: it waits for GROBID to
answer, sends the same request for every PDF, tries a busy GROBID again,
and reports a GROBID that never answers as unavailable, not as a bad PDF."""

import pytest
import requests

from scriptorium_server.grobid import Grobid, GrobidUnavailable
from scriptorium_server.tei import GROBID_FORM


class Reply:
    def __init__(self, status=200, body=b"", text=""):
        self.status_code, self.content, self.text = status, body, text
        self.ok = status < 400

    def json(self):
        return {"version": "0.9.1"}


class FakeSession:
    def __init__(self, alive=(True,), posts=((200, b"<TEI/>"),)):
        self.alive = list(alive)
        self.posts = list(posts)
        self.sent: list[tuple[str, list]] = []

    def get(self, url, timeout):
        if url.endswith("/api/version"):
            return Reply(text="0.9.1")
        up = self.alive.pop(0) if len(self.alive) > 1 else self.alive[0]
        if up is None:
            raise requests.ConnectionError("refused")
        return Reply(text="true" if up else "false")

    def post(self, url, files, data, timeout):
        self.sent.append((url, data))
        status, body = self.posts.pop(0)
        return Reply(status, body)


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4")
    return path


def test_parse_sends_the_fixed_request_and_returns_the_tei(pdf):
    session = FakeSession()
    client = Grobid("http://grobid:8070/", session=session, pause=0)
    assert client.parse(pdf) == (200, b"<TEI/>")
    assert session.sent == [("http://grobid:8070/api/processFulltextDocument", list(GROBID_FORM))]
    assert client.version() == "0.9.1"


def test_parse_waits_until_grobid_answers(pdf):
    session = FakeSession(alive=(None, False, True))
    assert Grobid("http://g", session=session, pause=0).parse(pdf)[0] == 200


def test_a_grobid_that_never_answers_is_unavailable(pdf):
    client = Grobid("http://g", session=FakeSession(alive=(None,)), wait_seconds=0, pause=0)
    with pytest.raises(GrobidUnavailable, match="did not answer"):
        client.parse(pdf)


def test_a_busy_grobid_is_asked_again(pdf):
    session = FakeSession(posts=((503, b""), (200, b"<TEI/>")))
    assert Grobid("http://g", session=session, pause=0).parse(pdf) == (200, b"<TEI/>")
    assert len(session.sent) == 2


def test_a_refused_pdf_returns_grobids_reason(pdf):
    session = FakeSession(posts=((500, b"[NO_BLOCKS] no text"),))
    assert Grobid("http://g", session=session, pause=0).parse(pdf) == (500, b"[NO_BLOCKS] no text")


def test_tests_never_reach_a_real_grobid(pdf):
    with pytest.raises(GrobidUnavailable):
        Grobid("http://127.0.0.1:8070", wait_seconds=0).parse(pdf)
