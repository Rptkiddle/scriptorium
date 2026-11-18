"""The Zotero tools against a fake plugin and a fake server: items are added
once, with their PDFs where the server hands them out; PDFs are compared on
this machine and sent, after confirmation, with their identifier only; and
without Zotero nothing is done."""

import hashlib
import json

import httpx
import pytest

from scriptorium_client import zotero
from scriptorium_client.http import Server, ServerError
from scriptorium_client.tools import Tools

PDF = b"%PDF-1.4 the held copy"


def item(key, doi=None, arxiv=None, pdf=False, **more):
    return {"key": key, "type": "journalArticle", "title": f"Paper {key}", "doi": doi,
            "arxiv_id": arxiv, "abstract": "An abstract.", "date": "2024-05-01",
            "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
            "preprints": [], "pdf_held": pdf,
            "details": {"volume": "7", "pages": "1-9", "key": key, "tags": [{"tag": "x"}],
                        "collections": ["ABCD1234"], "extra": "a line", "citationKey": "ck"},
            **more}


class FakeServer:
    def __init__(self, items, held=(), pdf_download=True, pdf_upload=True):
        self.items = {i["key"]: i for i in items}
        self.held, self.switches = list(held), {"pdf_download": pdf_download, "pdf_upload": pdf_upload}
        self.uploads, self.calls = [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/v1/health":
            return httpx.Response(200, json={"status": "ok", "server": "0.1.0", "items": 1,
                                             "fulltexts": 1, **self.switches})
        if path == "/v1/files/held":
            return httpx.Response(200, json={"count": len(self.held), "items": self.held})
        if request.method == "POST" and path == "/v1/files":
            self.uploads.append({"params": dict(request.url.params), "body": request.content,
                                 "type": request.headers["content-type"]})
            return httpx.Response(200, json={"upload": len(self.uploads), "status": "waiting"})
        if path.startswith("/v1/files/"):
            return httpx.Response(200, content=PDF, headers={
                "content-disposition": f'attachment; filename="{path.rsplit("/", 1)[-1]}.pdf"',
                "x-scriptorium-sha256": hashlib.sha256(PDF).hexdigest()})
        if path.startswith("/v1/items/"):
            found = self.items.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=found) if found else httpx.Response(
                404, json={"error": "no such item"})
        return httpx.Response(404, json={"error": "unexpected"})


class FakeBridge:
    """Stands for the plugin: records every call it gets."""

    def __init__(self, identifiers=(), pdfs=(), running=True):
        self.held, self.listed, self.running = set(identifiers), list(pdfs), running
        self.calls, self.created, self.imported = [], [], []

    def _check(self, name):
        self.calls.append(name)
        if not self.running:
            raise zotero.ZoteroUnavailable("Zotero is not running on this machine. Open Zotero,"
                                           " then ask again.")

    def identifiers(self):
        self._check("identifiers")
        return set(self.held)

    def create(self, items):
        self._check("create")
        self.created += items
        return {"created": {str(i): f"Z{len(self.created) - len(items) + i}" for i in range(len(items))},
                "failed": {}}

    def import_pdf(self, parent_key, path):
        self._check("import_pdf")
        with open(path, "rb") as f:
            self.imported.append((parent_key, f.read()))
        return "ATT1"

    def pdfs(self, collections=None):
        self._check("pdfs")
        return {"pdfs": self.listed, "without_identifier": 2, "missing_file": 0}


def make(tmp_path, server, bridge):
    config = {"server": "https://scriptorium.test", "api_key": "scr_key", "reply_chars": 4000,
              "results_dir": str(tmp_path / "client" / "results"), "pdf_dir": str(tmp_path / "pdfs")}
    return Tools(config, Server(config, transport=httpx.MockTransport(server)),
                 zotero=lambda: {"zotero_running": bridge.running, "bridge_plugin": bridge.running},
                 bridge=bridge)


def test_add_skips_items_already_present(tmp_path):
    server = FakeServer([item("K1", doi="10.1/A"), item("K2", doi="10.1/b"),
                         item("K3", arxiv="2501.00001", preprints=["doi:10.9/pre"]),
                         item("K4", doi="10.1/b")])
    bridge = FakeBridge(identifiers={"doi:10.1/a", "doi:10.9/pre"})
    report = make(tmp_path, server, bridge).zotero_add_items(["K1", "K2", "K3", "K4", "K2"])
    assert report["already_in_zotero"] == 3  # K1 held, K3 by its preprint, K4 the same DOI as K2
    assert [a["key"] for a in report["added"]] == ["K2"] and report["failed"] == []
    assert [i["DOI"] for i in bridge.created] == ["10.1/b"]


def test_add_never_updates_or_deletes(tmp_path):
    server = FakeServer([item("K1", doi="10.1/a", pdf=True)])
    bridge = FakeBridge()
    report = make(tmp_path, server, bridge).zotero_add_items(["K1"])
    assert set(bridge.calls) == {"identifiers", "create", "import_pdf"}  # all the plugin can do
    assert bridge.imported == [("Z0", PDF)] and report["pdfs_attached"] == 1
    assert not list((tmp_path / "pdfs").glob("*.pdf"))  # the copy made for Zotero is removed
    created = bridge.created[0]
    assert created["itemType"] == "journalArticle" and created["volume"] == "7"
    assert not {"key", "tags", "collections", "extra", "citationKey"} & set(created)


def test_add_without_pdfs_when_the_server_hands_none_out(tmp_path):
    server = FakeServer([item("K1", doi="10.1/a", pdf=True)], pdf_download=False)
    bridge = FakeBridge()
    report = make(tmp_path, server, bridge).zotero_add_items(["K1"])
    assert len(report["added"]) == 1 and report["pdfs_attached"] == 0 and bridge.imported == []
    assert "does not hand out PDFs" in report["pdfs"]
    assert not any(path.startswith("/v1/files/") for _, path in server.calls)


def test_add_goes_in_batches_and_reports_an_unknown_key(tmp_path, monkeypatch):
    monkeypatch.setattr("scriptorium_client.tools.ADD_BATCH", 2)
    server = FakeServer([item(f"K{n}", doi=f"10.1/{n}") for n in range(3)])
    bridge = FakeBridge()
    tools = make(tmp_path, server, bridge)
    first = tools.zotero_add_items(["K0", "nope", "K1", "K2"])
    assert len(first["added"]) == 2 and first["remaining"] == 1
    assert first["failed"] == [{"key": "nope", "reason": "no such item in the collection"}]
    bridge.held |= {"doi:10.1/0", "doi:10.1/1"}  # Zotero now holds the first two
    second = tools.zotero_add_items(["K0", "K1", "K2"])
    assert [a["key"] for a in second["added"]] == ["K2"] and second["remaining"] == 0


def pdf_files(tmp_path, n):
    folder = tmp_path / "storage"
    folder.mkdir()
    paths = []
    for i in range(n):
        path = folder / f"p{i}.pdf"
        path.write_bytes(b"%PDF-1.4 paper " + str(i).encode())
        paths.append(path)
    return paths


def test_donate_compares_locally(tmp_path):
    paths = pdf_files(tmp_path, 4)
    listed = [{"path": str(paths[0]), "identifiers": ["doi:10.1/held"]},
              {"path": str(paths[1]), "identifiers": ["arxiv:2501.00001"]},
              {"path": str(paths[2]), "identifiers": ["doi:10.1/new", "arxiv:2501.00002"]},
              {"path": str(paths[3]), "identifiers": ["doi:10.1/same-file"]}]
    held = [{"key": "A", "doi": "10.1/HELD", "arxiv_id": None, "md5": []},
            {"key": "B", "doi": None, "arxiv_id": "2501.00001", "md5": []},
            {"key": "C", "doi": None, "arxiv_id": None,
             "md5": [hashlib.md5(paths[3].read_bytes()).hexdigest()]}]
    server = FakeServer([], held=held)
    report = make(tmp_path, server, FakeBridge(pdfs=listed)).zotero_pdf_upload()
    assert report["skipped_held"] == 3 and report["remaining"] == 1
    assert report["without_identifier"] == 2 and "1 PDFs would be sent" in report["note"]
    assert server.uploads == []  # nothing is sent before the member agrees
    assert not any(path.startswith("/v1/items") or path == "/v1/lookup" for _, path in server.calls)


def test_donate_sends_only_pdf_and_identifier(tmp_path):
    paths = pdf_files(tmp_path, 2)
    listed = [{"path": str(paths[0]), "identifiers": ["arxiv:2501.00002", "doi:10.1/new"]},
              {"path": str(paths[1]), "identifiers": ["arxiv:2501.00003"]}]
    server = FakeServer([])
    tools = make(tmp_path, server, FakeBridge(pdfs=listed))
    report = tools.zotero_pdf_upload(confirm=True)
    assert len(report["sent"]) == 2 and report["remaining"] == 0
    assert [u["params"] for u in server.uploads] == [{"identifier": "10.1/new"},
                                                     {"identifier": "arXiv:2501.00003"}]
    assert [u["body"] for u in server.uploads] == [p.read_bytes() for p in paths]
    assert {u["type"] for u in server.uploads} == {"application/pdf"}
    again = tools.zotero_pdf_upload(confirm=True)
    assert again["skipped_sent_before"] == 2 and len(server.uploads) == 2


def test_donate_is_refused_when_the_server_takes_no_pdfs(tmp_path):
    paths = pdf_files(tmp_path, 1)
    server = FakeServer([], pdf_upload=False)
    tools = make(tmp_path, server, FakeBridge(pdfs=[{"path": str(paths[0]), "identifiers": ["doi:10.1/x"]}]))
    with pytest.raises(ServerError, match="does not accept donated PDFs"):
        tools.zotero_pdf_upload(confirm=True)
    assert server.uploads == []


def test_zotero_closed_says_so_and_writes_nothing(tmp_path):
    server = FakeServer([item("K1", doi="10.1/a")])
    bridge = FakeBridge(running=False)
    tools = make(tmp_path, server, bridge)
    for call in (lambda: tools.zotero_add_items(["K1"]), lambda: tools.zotero_pdf_upload(confirm=True)):
        with pytest.raises(ServerError, match="Zotero is not running"):
            call()
    assert server.calls == [] and bridge.created == []  # the server was not even asked
    assert not (tmp_path / "client").exists()  # and nothing was written on this machine


def test_the_plugin_not_answering_is_told_apart_from_zotero_not_running():
    def post(url, **kwargs):
        if url.endswith("/scriptorium/ping") or "/scriptorium/" in url:
            return httpx.Response(404)
        raise AssertionError(url)

    def get(url, **kwargs):
        return httpx.Response(200)

    with pytest.raises(zotero.ZoteroUnavailable, match="scriptorium-bridge plugin does not answer"):
        zotero.Bridge(post=post, get=get).identifiers()

    def down(url, **kwargs):
        raise httpx.ConnectError("refused")

    with pytest.raises(zotero.ZoteroUnavailable, match="Zotero is not running"):
        zotero.Bridge(post=down, get=down).identifiers()


def test_a_recorded_arxiv_preprint_goes_into_extra():
    record = zotero.zotero_item(item("K1", doi="10.1/a", preprints=["arxiv:2501.00001", "doi:10.9/p"]))
    assert record["extra"] == "arXiv: 2501.00001" and record["DOI"] == "10.1/a"
    assert json.dumps(record)  # plain JSON for the plugin
