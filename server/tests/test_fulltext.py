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


# --- is this PDF the publication? ------------------------------------------------

from scriptorium_server.fulltext import mismatch  # noqa: E402

BODY = " ".join([PROSE + " one", PROSE + " two"])


def tei_of(title="", surnames=(), doi="", body=BODY, cited="") -> bytes:
    authors = "".join(f"<author><persName><surname>{s}</surname></persName></author>"
                      for s in surnames)
    idno = f'<idno type="DOI">{doi}</idno>' if doi else ""
    refs = (f'<div type="references"><listBibl><biblStruct><analytic><title>{cited}</title>'
            '</analytic></biblStruct></listBibl></div>') if cited else ""
    return ('<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt>'
            f'<title level="a" type="main">{title}</title></titleStmt><sourceDesc><biblStruct>'
            f'<analytic>{authors}<title>{title}</title></analytic>{idno}</biblStruct></sourceDesc>'
            '</fileDesc></teiHeader><text><body><div>'
            f'<p><s coords="1,1,1,1,1">{body}</s></p></div></body><back>{refs}</back></text></TEI>').encode()


TITLE = "Echo chambers in online news: a review"
CREATORS = [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}]


@pytest.mark.parametrize("tei_xml", [
    tei_of(TITLE),
    tei_of("Echo Chambers in Online News"),                     # subtitle missing
    tei_of("nature human behaviour", doi="10.1000/ABC"),        # journal name read; DOI agrees
    tei_of("", body=f"{TITLE} {BODY}"),                         # title at the start of the text
])
def test_the_right_publication_is_accepted(tei_xml):
    assert mismatch(tei_xml, TITLE, CREATORS, "10.1000/abc") is None


@pytest.mark.parametrize("tei_xml", [
    tei_of("A model of opinion dynamics", ["Lovelace"]),        # same author, other paper
    tei_of("Echo chambers in news online", ["Lovelace"]),       # same author, near title
    tei_of("nature human behaviour", doi="10.1000/other"),
    tei_of("A model of opinion dynamics", cited=TITLE),         # cites the item
    tei_of("A model of opinion dynamics",                       # cites it in a footnote far on
           body=" ".join([PROSE] * 20) + f" as shown in {TITLE}"),
    tei_of(""),
])
def test_another_publication_is_refused_with_a_reason(tei_xml):
    assert "title" in mismatch(tei_xml, TITLE, CREATORS, "10.1000/abc")


def test_a_short_title_on_the_front_is_not_enough():
    short = "Garbage in, garbage out?"
    assert mismatch(tei_of("Data quality in annotation", body=f"{short} {BODY}"), short, CREATORS)


# --- the full-text job ---------------------------------------------------------------

import pikepdf  # noqa: E402

from scriptorium_server import items as collection  # noqa: E402
from scriptorium_server import jobs, members  # noqa: E402
from scriptorium_server.embedding import FakeEmbedder  # noqa: E402
from scriptorium_server.filestore import FileStore, digest  # noqa: E402
from scriptorium_server.fulltext import Context, Rejected, process  # noqa: E402

RECORD = {"itemType": "journalArticle", "title": TITLE, "creators": CREATORS,
          "DOI": "10.1000/abc", "date": "2025"}


class FakeTeiGrobid(FakeGrobid):
    def __init__(self, tei_xml=None, **kwargs):
        super().__init__(reply=(200, tei_xml or tei_of(TITLE, ["Lovelace"], "10.1000/abc")),
                         **kwargs)


@pytest.fixture
def donation(tmp_path):
    def make(name="donated.pdf"):
        pdf = pikepdf.new()
        pdf.add_blank_page(page_size=(100, 100))
        pdf.docinfo["/Author"] = "Jane Doe"
        path = tmp_path / "holding" / name
        path.parent.mkdir(exist_ok=True)
        pdf.save(path)
        return path
    return make


@pytest.fixture
def ctx(tmp_path):
    def make(grobid=None, registry=None):
        return Context(grobid=grobid or FakeTeiGrobid(), tei=TeiStore(tmp_path / "tei"),
                       files=FileStore(tmp_path / "data"), embedder=FakeEmbedder(),
                       registry=registry)
    return make


def job(payload, member=None):
    return jobs.Job(1, "fulltext", payload, member, None)


def test_a_donated_pdf_becomes_passages_and_one_stripped_file(db, ctx, donation):
    key, _ = collection.admit(db, RECORD, "venue")
    db.commit()
    pdf = donation()
    context = ctx()
    summary = process(db, job({"file": str(pdf), "item": key}), context)
    assert "passages from the donated PDF" in summary
    rows = db.execute("SELECT idx, part, embedding IS NOT NULL AS has_vector, artefact"
                      " FROM passages WHERE item = %s ORDER BY idx", (key,)).fetchall()
    assert rows and all(r["has_vector"] or r["artefact"] for r in rows)
    (stored,) = db.execute("SELECT sha256, source_md5 FROM files WHERE item = %s", (key,)).fetchall()
    assert b"Jane Doe" not in context.files.path(stored["sha256"]).read_bytes()
    assert db.execute("SELECT has_fulltext FROM items WHERE key = %s", (key,)).fetchone()["has_fulltext"]
    assert not pdf.exists() and context.tei.usable(key, stored["source_md5"])
    assert process(db, job({"file": str(pdf), "item": key}), context).startswith("nothing to do")


def test_wrong_document_fails_and_is_not_kept(db, ctx, donation, tmp_path):
    key, _ = collection.admit(db, RECORD, "venue")
    member = members.add(db, "ada") and db.execute("SELECT id FROM members").fetchone()["id"]
    pdf = donation()
    wrong = FakeTeiGrobid(tei_of("A model of opinion dynamics", ["Smith"]))
    jobs.enqueue(db, "fulltext", {"file": str(pdf), "item": key}, member)
    db.commit()
    handlers = {"fulltext": lambda conn, j: process(conn, j, ctx(grobid=wrong))}
    jobs.run_worker(db, tmp_path / "runs", handlers, until_idle=True)
    row = db.execute("SELECT status, error, member FROM jobs").fetchone()
    assert row["status"] == "failed" and "not kept" in row["error"] and "title" in row["error"]
    assert row["member"] == member  # the member's progress shows it
    assert not pdf.exists()
    assert db.execute("SELECT count(*) AS n FROM files").fetchone()["n"] == 0
    assert db.execute("SELECT count(*) AS n FROM passages").fetchone()["n"] == 0
    assert not (tmp_path / "data" / "pdfs").exists() or not any((tmp_path / "data" / "pdfs").rglob("*.pdf"))


def test_donation_outside_collection_creates_item(db, ctx, donation):
    members.add(db, "ada")
    member = db.execute("SELECT id FROM members").fetchone()["id"]
    db.commit()
    asked = []

    def registry(kind, value):
        asked.append((kind, value))
        return dict(RECORD), None

    process(db, job({"file": str(donation()), "identifier": "https://doi.org/10.1000/ABC"}, member),
            ctx(registry=registry))
    assert asked == [("doi", "10.1000/abc")]
    item = db.execute("SELECT key, provenance_kind, provenance_member, has_fulltext,"
                      " embedding IS NOT NULL AS has_vector FROM items").fetchone()
    assert (item["provenance_kind"], item["provenance_member"], item["has_fulltext"]) == (
        "donation", member, True)
    assert item["has_vector"]  # item search can find it


def test_the_doi_on_the_first_page_names_the_publication_when_none_is_given(db, ctx, donation):
    key, _ = collection.admit(db, RECORD, "venue")
    db.commit()
    process(db, job({"file": str(donation())}), ctx())
    assert db.execute("SELECT item FROM files").fetchone()["item"] == key


def test_an_unknown_identifier_is_refused_and_the_file_let_go(db, ctx, donation):
    pdf = donation()
    with pytest.raises(Rejected, match="does not know"):
        process(db, job({"file": str(pdf), "identifier": "10.1000/nothing"}),
                ctx(registry=lambda kind, value: None))
    assert not pdf.exists()


def test_grobid_unavailable_keeps_the_file_for_another_try(db, ctx, donation):
    key, _ = collection.admit(db, RECORD, "venue")
    db.commit()
    pdf = donation()
    with pytest.raises(GrobidUnavailable):
        process(db, job({"file": str(pdf), "item": key}), ctx(grobid=FakeTeiGrobid(start_error="down")))
    assert pdf.exists()


def test_the_awaited_seed_pdf_is_stored_without_parsing(db, ctx, donation):
    key, _ = collection.admit(db, RECORD, "venue")
    pdf = donation()
    db.execute("INSERT INTO pending_files (md5, item) VALUES (%s, %s)", (digest(pdf, "md5"), key))
    db.commit()
    grobid = FakeTeiGrobid()
    process(db, job({"file": str(pdf), "item": key}), ctx(grobid=grobid))
    assert grobid.calls == []
    assert db.execute("SELECT count(*) AS n FROM pending_files").fetchone()["n"] == 0
    assert db.execute("SELECT item FROM files").fetchone()["item"] == key


def test_an_item_with_a_pdf_keeps_it(db, ctx, donation):
    key, _ = collection.admit(db, RECORD, "venue")
    db.execute("INSERT INTO files (sha256, item, bytes) VALUES ('s0', %s, 1)", (key,))
    db.commit()
    pdf = donation()
    assert "has a PDF already" in process(db, job({"file": str(pdf), "item": key}), ctx())
    assert not pdf.exists()
