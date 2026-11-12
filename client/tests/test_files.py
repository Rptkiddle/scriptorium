"""The file tools against a fake server: download saves the bytes the
server holds; donating a folder skips what the server holds and what was
sent before, sends in batches and can be resumed; progress is the
server's; every request is in the contract."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from scriptorium_client import tools as client_tools
from scriptorium_client.http import Server, ServerError
from scriptorium_client.tools import Tools

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / "api" / "openapi.json").read_text())
PDF = b"%PDF-1.4 the held copy"


class FakeFiles:
    def __init__(self, held_md5=()):
        self.calls, self.donated, self.limit_after = [], [], None
        self.held_md5 = list(held_md5)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path, dict(request.url.params)))
        if request.method == "POST" and path == "/v1/files":
            if self.limit_after is not None and len(self.donated) >= self.limit_after:
                return httpx.Response(429, json={"error": "rate limit"})
            assert request.headers["content-type"] == "application/pdf"
            if request.content.endswith(b"BAD"):
                return httpx.Response(413, json={"error": "the PDF is larger than 100 MB"})
            self.donated.append(request.content)
            return httpx.Response(200, json={"donation": len(self.donated), "status": "waiting"})
        if path == "/v1/files/held":
            return httpx.Response(200, json={"count": 1, "items": [
                {"key": "K0001", "doi": "10.1000/held", "arxiv_id": None, "md5": self.held_md5}]})
        if path == "/v1/donations":
            return httpx.Response(200, json={"total": 1, "donations": [{"donation": 1, "state": "stored"}]})
        if path == "/v1/files/K0001":
            return httpx.Response(200, content=PDF, headers={
                "content-disposition": 'attachment; filename="lovelaceNotes2025.pdf"',
                "x-scriptorium-sha256": hashlib.sha256(PDF).hexdigest()})
        return httpx.Response(404, json={"error": "no PDF is held for that item"})


def make_tools(fake, tmp_path):
    config = {"server": "https://scriptorium.test", "api_key": "scr_key", "reply_chars": 4000,
              "results_dir": str(tmp_path / "client" / "results"), "pdf_dir": str(tmp_path / "pdfs")}
    return Tools(config, Server(config, transport=httpx.MockTransport(fake)),
                 zotero=lambda: {"zotero_running": False, "bridge_plugin": False})


def folder_of(tmp_path, n):
    folder = tmp_path / "papers"
    (folder / "sub").mkdir(parents=True)
    paths = [(folder if i % 2 else folder / "sub") / f"p{i}.pdf" for i in range(n)]
    for i, p in enumerate(paths):
        p.write_bytes(b"%PDF-1.4 paper " + str(i).encode())
    (folder / "notes.txt").write_text("not a pdf")
    return folder, paths


def test_download_saves_the_same_bytes(tmp_path):
    tools = make_tools(FakeFiles(), tmp_path)
    saved = tools.download_pdf("K0001")
    assert Path(saved["saved_to"]) == tmp_path / "pdfs" / "lovelaceNotes2025.pdf"
    assert Path(saved["saved_to"]).read_bytes() == PDF
    with pytest.raises(ServerError, match="no PDF is held"):
        tools.download_pdf("K0002")


def test_donate_folder_skips_held(tmp_path):
    folder, paths = folder_of(tmp_path, 3)
    held = hashlib.md5(paths[0].read_bytes()).hexdigest()
    fake = FakeFiles(held_md5=[held])
    report = make_tools(fake, tmp_path).donate_pdf(str(folder))
    assert (report["pdfs"], report["skipped_held"], len(report["sent"]), report["remaining"]) == (3, 1, 2, 0)
    assert paths[0].read_bytes() not in fake.donated
    again = make_tools(fake, tmp_path).donate_pdf(str(folder))  # nothing is sent twice
    assert (again["skipped_held"], again["skipped_sent_before"], again["sent"]) == (1, 2, [])


def test_a_large_folder_goes_in_batches_and_resumes_after_the_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(client_tools, "DONATE_BATCH", 3)
    folder, paths = folder_of(tmp_path, 7)
    fake = FakeFiles()
    tools = make_tools(fake, tmp_path)
    first = tools.donate_pdf(str(folder))
    assert len(first["sent"]) == 3 and first["remaining"] == 4 and "again" in first["note"]
    fake.limit_after = 4  # the server's rate per member is reached after one more
    second = tools.donate_pdf(str(folder))
    assert len(second["sent"]) == 1 and second["remaining"] == 3 and "in a minute" in second["note"]
    fake.limit_after = None
    third = tools.donate_pdf(str(folder))
    assert len(third["sent"]) == 3 and third["remaining"] == 0
    assert len(fake.donated) == len(set(fake.donated)) == 7


def test_one_pdf_is_sent_with_its_identifier_unless_held(tmp_path):
    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(b"%PDF-1.4 one")
    fake = FakeFiles()
    tools = make_tools(fake, tmp_path)
    assert tools.donate_pdf(str(pdf), "https://doi.org/10.1000/held")["skipped_held"] == 1
    tools.donate_pdf(str(pdf), "10.1000/new")
    assert fake.calls[-1][2] == {"identifier": "10.1000/new"}


def test_progress_and_every_file_request_follow_the_contract(tmp_path):
    fake = FakeFiles()
    tools = make_tools(fake, tmp_path)
    assert tools.donation_progress()["donations"][0]["state"] == "stored"
    tools.download_pdf("K0001")
    folder, _ = folder_of(tmp_path, 1)
    tools.donate_pdf(str(folder))
    for method, path, params in fake.calls:
        template = "/v1/files/{key}" if path == "/v1/files/K0001" else path
        operation = CONTRACT["paths"][template][method.lower()]
        allowed = {p["name"] for p in operation.get("parameters", [])}
        assert set(params) <= allowed, (path, params)


def test_a_refused_file_is_reported_and_the_rest_still_goes(tmp_path):
    folder, paths = folder_of(tmp_path, 4)
    paths[1].write_bytes(b"%PDF-1.4 too big BAD")
    fake = FakeFiles()
    tools = make_tools(fake, tmp_path)
    report = tools.donate_pdf(str(folder))
    assert len(report["sent"]) == 3 and report["remaining"] == 0
    assert [r["file"] for r in report["refused"]] == ["p1.pdf"] and "100 MB" in report["refused"][0]["reason"]
    again = tools.donate_pdf(str(folder))
    assert (again["skipped_sent_before"], again["skipped_refused_before"], again["sent"]) == (3, 1, [])


def test_an_unreachable_server_stops_the_batch_without_recording_it(tmp_path):
    folder, paths = folder_of(tmp_path, 2)

    def down(request):
        if request.url.path == "/v1/files/held":
            return httpx.Response(200, json={"count": 0, "items": []})
        raise httpx.ConnectError("no route")

    tools = make_tools(down, tmp_path)
    with pytest.raises(ServerError, match="cannot be reached"):
        tools.donate_pdf(str(folder))
    assert tools._sent() == {}  # nothing recorded: the next call tries again
