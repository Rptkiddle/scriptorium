"""Files through the API: donating replies at once and is processed by the
worker; a member sees only their own donations; a held PDF downloads as the
same bytes; the list of held PDFs; donated text becomes searchable. No PDF
ever enters the usage log."""

import json

import pikepdf
import pytest
from fastapi.testclient import TestClient

from scriptorium_server import db as database, jobs, members, settings
from scriptorium_server.api import create_app
from scriptorium_server.embedding import FakeEmbedder
from scriptorium_server.filestore import FileStore
from scriptorium_server.fulltext import Context, process
from scriptorium_server.items import admit
from scriptorium_server.tei import TeiStore

TITLE = "Echo chambers in online news: a review"
RECORD = {"itemType": "journalArticle", "title": TITLE, "DOI": "10.1000/abc", "date": "2025",
          "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}]}
TEXT = ("the quick model of the network shows how the group and its members act in the world"
        " of zebra crossings and their many uses")
TEI = ('<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt>'
       f'<title level="a" type="main">{TITLE}</title></titleStmt></fileDesc></teiHeader><text>'
       f'<body><div><p><s coords="1,1,1,1,1">{TEXT}</s></p></div></body><back/></text></TEI>').encode()


class FakeGrobid:
    def version(self):
        return "test"

    def parse(self, pdf):
        return 200, TEI


def pdf_bytes(note="a") -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(100, 100))
    pdf.docinfo["/Title"] = note
    from io import BytesIO
    out = BytesIO()
    pdf.save(out)
    return out.getvalue()


@pytest.fixture
def world(db, empty_schema, tmp_path):
    key, _ = admit(db, RECORD, "venue")
    ada, grace = members.add(db, "ada"), members.add(db, "grace")
    db.commit()
    path = tmp_path / "s.toml"
    settings.set_value("data.folder", str(tmp_path / "data"), path)
    settings.set_value("limits.upload_megabytes", 1, path)
    app = create_app(lambda: database.connect(*empty_schema), FakeEmbedder(), settings.load(path))

    def client(api_key):
        return TestClient(app, headers={"Authorization": f"Bearer {api_key}",
                                        "X-Scriptorium-Client": "0.1.0"})

    ctx = Context(grobid=FakeGrobid(), tei=TeiStore(tmp_path / "data" / "tei"),
                  files=FileStore(tmp_path / "data"), embedder=FakeEmbedder())

    def work():
        jobs.run_worker(db, tmp_path / "data" / "runs",
                        {"fulltext": lambda conn, job: process(conn, job, ctx)}, until_idle=True)

    return type("World", (), {"ada": client(ada), "grace": client(grace), "db": db, "key": key,
                              "work": staticmethod(work), "store": ctx.files})


def donate(client, data, **params):
    return client.post("/v1/files", content=data, params=params,
                       headers={"Content-Type": "application/pdf"})


def last_log(db):
    return db.execute("SELECT request, inputs, reply FROM usage_log ORDER BY id DESC LIMIT 1").fetchone()


def test_upload_replies_before_processing(world):
    data = pdf_bytes()
    reply = donate(world.ada, data, identifier="https://doi.org/10.1000/ABC")
    assert reply.status_code == 200 and reply.json()["status"] == "waiting"
    job = world.db.execute("SELECT status, payload FROM jobs").fetchone()
    assert job["status"] == "waiting" and job["payload"]["identifier"] == "10.1000/abc"
    logged = last_log(world.db)
    assert logged["inputs"]["bytes"] == len(data) and len(logged["inputs"]["sha256"]) == 64
    assert "%PDF" not in json.dumps(logged["inputs"]) + json.dumps(logged["reply"])


def test_upload_refuses_what_is_not_a_pdf_or_too_large(world):
    assert donate(world.ada, b"hello").status_code == 400
    too_big = b"%PDF-" + b"0" * (1024 * 1024 + 10)
    refused = donate(world.ada, too_big, identifier="10.1000/abc")
    assert refused.status_code == 413 and "1 MB" in refused.json()["error"]
    assert donate(world.ada, pdf_bytes(), identifier="not an id").status_code == 400
    assert world.db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0


def test_progress_shows_only_own_donations(world):
    donate(world.ada, pdf_bytes("a"), identifier="10.1000/abc")
    donate(world.grace, pdf_bytes("g"), item=world.key)
    world.work()
    mine = world.ada.get("/v1/donations").json()
    assert mine["total"] == 1
    (only,) = mine["donations"]
    assert only["state"] == "stored" and only["item"] == world.key and only["identifier"] == "10.1000/abc"
    theirs = world.grace.get("/v1/donations").json()["donations"]
    assert [d["state"] for d in theirs] == ["already held"]  # ada's PDF was stored first
    assert "ada" not in json.dumps(theirs) and "grace" not in json.dumps(mine)


def test_a_rejected_donation_shows_its_reason(world):
    admit(world.db, {**RECORD, "title": "A different paper altogether", "DOI": "10.1000/zzz"}, "venue")
    world.db.commit()
    donate(world.ada, pdf_bytes(), identifier="10.1000/zzz")
    world.work()
    (d,) = world.ada.get("/v1/donations").json()["donations"]
    assert d["state"] == "rejected" and "not kept" in d["result"] and "title" in d["result"]


def test_download_saves_the_same_bytes(world):
    donate(world.ada, pdf_bytes(), identifier="10.1000/abc")
    world.work()
    sha = world.db.execute("SELECT sha256 FROM files").fetchone()["sha256"]
    reply = world.grace.get(f"/v1/files/{world.key}")
    assert reply.status_code == 200 and reply.headers["content-type"] == "application/pdf"
    assert reply.content == world.store.path(sha).read_bytes()
    assert reply.headers["x-scriptorium-sha256"] == sha
    assert last_log(world.db)["reply"] == {"key": world.key, "sha256": sha,
                                           "bytes": len(reply.content)}


def test_download_of_an_item_without_a_pdf_says_so(world):
    world.db.execute("UPDATE items SET has_fulltext = true")
    world.db.commit()
    reply = world.ada.get(f"/v1/files/{world.key}")
    assert reply.status_code == 404 and "has not been donated yet" in reply.json()["error"]
    assert world.ada.get("/v1/files/NOSUCHKEY").status_code == 404


def test_held_lists_pdfs_and_the_log_keeps_only_the_count(world):
    assert world.ada.get("/v1/files/held").json() == {"count": 0, "items": []}
    donate(world.ada, pdf_bytes(), identifier="10.1000/abc")
    world.work()
    held = world.ada.get("/v1/files/held").json()
    assert held["count"] == 1 and held["items"][0]["doi"] == "10.1000/abc"
    assert len(held["items"][0]["md5"]) == 1
    assert last_log(world.db)["reply"] == {"count": 1}


def test_passage_search_finds_donated_text(world):
    donate(world.ada, pdf_bytes(), identifier="10.1000/abc")
    world.work()
    found = world.grace.post("/v1/search/passages", json={"query": "zebra crossings"}).json()
    assert found["passages"] and found["passages"][0]["item"] == world.key


def test_nothing_is_received_or_served_without_a_key(world, tmp_path):
    anonymous = TestClient(world.ada.app)
    assert donate(anonymous, pdf_bytes(), identifier="10.1000/abc").status_code == 401
    for path in ("/v1/files/held", "/v1/donations", f"/v1/files/{world.key}"):
        assert anonymous.get(path).status_code == 401
    assert not (tmp_path / "data" / "holding").exists()
    assert world.db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0


def test_a_declared_size_over_the_limit_is_refused_unread(world):
    reply = world.ada.post("/v1/files", content=b"%PDF-", params={"identifier": "10.1000/abc"},
                           headers={"Content-Type": "application/pdf",
                                    "Content-Length": str(50 * 1024 * 1024)})
    assert reply.status_code == 413
    assert "sha256" not in last_log(world.db)["inputs"]  # the body was never read


def test_a_pdf_without_a_doi_is_matched_by_its_title(world):
    donate(world.ada, pdf_bytes())  # no identifier; the fake GROBID reads no DOI, only the title
    world.work()
    (d,) = world.ada.get("/v1/donations").json()["donations"]
    assert d["state"] == "stored" and d["item"] == world.key


def test_a_failed_donation_tells_the_member_plainly(world):
    world.db.execute("INSERT INTO jobs (kind, payload, member, status, error) SELECT 'fulltext',"
                     " '{}', id, 'failed', 'GrobidUnavailable: GROBID at http://grobid:8070 did not"
                     " answer' FROM members WHERE name = 'ada'")
    world.db.commit()
    (d,) = world.ada.get("/v1/donations").json()["donations"]
    assert d["state"] == "failed" and "grobid:8070" not in d["result"]
    assert "administrator can run it again" in d["result"]
