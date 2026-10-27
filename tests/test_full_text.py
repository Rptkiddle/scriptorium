"""Full text end to end with a fake GROBID: a sync has GROBID read new PDFs
first and embeds afterwards, reuses a stored TEI of the same PDF, records
a PDF GROBID rejects, waits when GROBID cannot start; passage search skips
artefacts and returns context within the cap."""

from dataclasses import replace

import pytest

from scriptorium.config import Config
from scriptorium.grobid import GrobidUnavailable
from scriptorium.index import HubIndex
from scriptorium.parsing import tei_store
from scriptorium.search import HubSearch
from scriptorium.service import sync_library
from scriptorium.zotero import PaperRecord
from tests.test_store import Embedder

PROSE = ("the model of the network is a way to see how the group and its members"
         " act in the world")
TABLE = " ".join(f"{i}.{i} 0.{i}" for i in range(30))


def tei(*paragraphs: str) -> bytes:
    body = "".join(f'<p><s coords="{n},1,1,1,1">{t}</s></p>' for n, t in enumerate(paragraphs, 1))
    return ('<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader/><text>'
            f"<body><div>{body}</div></body><back/></text></TEI>").encode()


class FakeGrobid:
    def __init__(self, replies=None, start_error=None):
        self.replies = replies or {}
        self.start_error = start_error
        self.calls: list[str] = []
        self.running = False

    def ensure(self):
        if self.start_error:
            raise GrobidUnavailable(self.start_error)

    def version(self):
        return "test"

    def parse(self, pdf):
        self.ensure()
        self.calls.append(str(pdf))
        return self.replies.get(str(pdf), (200, tei(PROSE + " one", TABLE, PROSE + " two")))


class Reader:
    def __init__(self, records):
        self.records = records

    def library_version(self):
        return 1

    def collections(self):
        return []

    def iter_papers(self):
        return self.records

    def all_item_keys(self):
        return {r.item_key for r in self.records}


def paper(tmp_path, key, content=b"%PDF-1"):
    pdf = tmp_path / f"{key}.pdf"
    pdf.write_bytes(content)
    return PaperRecord(item_key=key, version=1, item_type="journalArticle", title=f"T {key}",
                       creators="Kiddle", year="2026", doi="", venue="V",
                       citation_key=key.lower(), date_added="2026-10-24T10:00:00Z",
                       pdf_key="", pdf_path=str(pdf), pdf_status="ok")


@pytest.fixture
def setup(tmp_path):
    cfg = Config(index_dir=tmp_path / "data", embedding_dim=4)
    cfg.index_dir.mkdir()
    hub = HubIndex(cfg, embedder=Embedder())
    return cfg, hub


def test_grobid_reads_new_pdfs_and_artefacts_get_no_vector(tmp_path, setup):
    cfg, hub = setup
    grobid = FakeGrobid()
    rec = paper(tmp_path, "A")
    summary = sync_library(Reader([rec]), hub, cfg, grobid=grobid)
    assert summary["added"] == 1 and grobid.calls == [rec.pdf_path]
    rows = hub.store.query("SELECT part, pages, artefact, vec IS NULL AS novec FROM chunks"
                           " ORDER BY chunk_index")
    assert [(r["pages"], r["artefact"], r["novec"]) for r in rows] == [
        ("1", 0, 0), ("2", 1, 1), ("3", 0, 0)]
    assert tei_store(cfg).usable("A", hub.state.items["A"]["content_hash"])
    hits = HubSearch(cfg, hub=hub, embedder=Embedder()).search("q", k=10)
    assert len(hits) == 2 and all(h.part == "body" for h in hits)


def test_a_stored_tei_of_the_same_pdf_is_used_without_grobid(tmp_path, setup):
    cfg, hub = setup
    rec = paper(tmp_path, "B")
    from paperqa.utils import md5sum
    tei_store(cfg).put("B", rec.pdf_path, md5sum(rec.pdf_path), "0.9.1", 200, 1.0,
                       tei=tei(PROSE))
    grobid = FakeGrobid(start_error="must not be needed")
    assert sync_library(Reader([rec]), hub, cfg, grobid=grobid)["added"] == 1
    assert grobid.calls == []


def test_a_pdf_grobid_rejects_is_a_failure_and_not_retried(tmp_path, setup):
    cfg, hub = setup
    rec = paper(tmp_path, "C")
    grobid = FakeGrobid({rec.pdf_path: (500, b"[NO_BLOCKS] PDF parsing resulted in empty content")})
    summary = sync_library(Reader([rec]), hub, cfg, grobid=grobid)
    assert summary["failed"] == 1 and "NO_BLOCKS" in hub.state.failed["C"]
    sync_library(Reader([replace(rec, version=2)]), hub, cfg, grobid=grobid)
    assert len(grobid.calls) == 1  # same PDF: not sent again


def test_no_body_text_is_a_failure(tmp_path, setup):
    cfg, hub = setup
    rec = paper(tmp_path, "E")
    empty = (b'<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body/></text></TEI>')
    sync_library(Reader([rec]), hub, cfg, grobid=FakeGrobid({rec.pdf_path: (200, empty)}))
    assert "no body text" in hub.state.failed["E"] and "E" not in hub.state.items
    assert "a scan, or a short item" in hub.state.failed["E"]  # both causes named


def test_papers_wait_when_grobid_cannot_start(tmp_path, setup):
    cfg, hub = setup
    rec = paper(tmp_path, "D")
    summary = sync_library(Reader([rec]), hub, cfg, grobid=FakeGrobid(start_error="no Java"))
    assert summary["grobid_unavailable"] == "no Java" and summary["waiting_for_grobid"] == 1
    assert "D" not in hub.state.failed and "D" not in hub.state.items


def test_context_returns_neighbours_in_the_same_part_and_the_cap_holds(tmp_path, setup):
    cfg, hub = setup
    paras = [f"{PROSE} number {i}" for i in range(5)]
    rec = paper(tmp_path, "F")
    sync_library(Reader([rec]), hub, cfg, grobid=FakeGrobid({rec.pdf_path: (200, tei(*paras))}))
    search = HubSearch(cfg, hub=hub, embedder=Embedder())
    hit = next(h for h in search.search("q", k=5, context=1) if h.chunk_index == 2)
    assert hit.before == [paras[1]] and hit.after == [paras[3]]
    assert "before" not in search.search("q", k=1)[0].to_dict()
    with pytest.raises(ValueError, match="at most 20"):
        search.search("q", k=7, context=1)
    with pytest.raises(ValueError, match="context"):
        search.search("q", k=1, context=4)
