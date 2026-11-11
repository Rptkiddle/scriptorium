"""From a PDF to passages with a fake GROBID: the TEI is kept and reused, a
PDF GROBID rejects or one without body text is a failure with its reason,
and an unavailable GROBID is reported as such."""

import pytest

from scriptorium_server.fulltext import ParseError, parse_pdf
from scriptorium_server.grobid import GrobidUnavailable
from scriptorium_server.tei import TeiStore

PROSE = ("the model of the network is a way to see how the group and its members"
         " act in the world")
TABLE = " ".join(f"{i}.{i} 0.{i}" for i in range(30))


def tei(*paragraphs: str) -> bytes:
    body = "".join(f'<p><s coords="{n},1,1,1,1">{t}</s></p>' for n, t in enumerate(paragraphs, 1))
    return ('<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader/><text>'
            f"<body><div>{body}</div></body><back/></text></TEI>").encode()


class FakeGrobid:
    def __init__(self, reply=None, start_error=None):
        self.reply = reply or (200, tei(PROSE + " one", TABLE, PROSE + " two"))
        self.start_error = start_error
        self.calls: list[str] = []

    def version(self):
        return "test"

    def parse(self, pdf):
        if self.start_error:
            raise GrobidUnavailable(self.start_error)
        self.calls.append(str(pdf))
        return self.reply


@pytest.fixture
def store(tmp_path):
    return TeiStore(tmp_path / "tei")


def test_grobid_reads_a_new_pdf_and_artefacts_are_marked(store):
    grobid = FakeGrobid()
    found = parse_pdf("K1", "/pdfs/k1.pdf", "hash-1", store, grobid)
    assert grobid.calls == ["/pdfs/k1.pdf"]
    assert [p.artefact for p in found] == [False, True, False]
    assert found[0].text == PROSE + " one"


def test_a_stored_tei_of_the_same_pdf_is_used_without_grobid(store):
    parse_pdf("K1", "/pdfs/k1.pdf", "hash-1", store, FakeGrobid())
    again = FakeGrobid()
    assert len(parse_pdf("K1", "/pdfs/k1.pdf", "hash-1", store, again)) == 3
    assert again.calls == []
    parse_pdf("K1", "/pdfs/k1.pdf", "hash-2", store, again)  # another file: read again
    assert again.calls == ["/pdfs/k1.pdf"]


def test_a_pdf_grobid_rejects_fails_with_grobids_reason(store):
    with pytest.raises(ParseError) as failure:
        parse_pdf("K1", "/pdfs/k1.pdf", "hash-1", store, FakeGrobid((500, b"[NO_BLOCKS] nothing")))
    assert failure.value.item_key == "K1" and "[NO_BLOCKS]" in failure.value.reason
    assert not store.usable("K1", "hash-1")


def test_no_body_text_is_a_failure(store):
    with pytest.raises(ParseError) as failure:
        parse_pdf("K1", "/pdfs/k1.pdf", "hash-1", store, FakeGrobid((200, tei())))
    assert "a scan" in failure.value.reason


def test_an_unavailable_grobid_is_not_a_parse_failure(store):
    with pytest.raises(GrobidUnavailable):
        parse_pdf("K1", "/pdfs/k1.pdf", "hash-1", store, FakeGrobid(start_error="no Java"))
    assert store.latest() == {}
