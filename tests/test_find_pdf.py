"""Finding one item's PDF through Zotero: the file ends up the way the library
keeps its PDFs, only final versions are taken by default, and nothing is left
behind."""

import json
import threading

from scriptorium.config import Config
from scriptorium.settings import save_settings
from scriptorium.zotero_bridge import BridgeTimeout
from scriptorium.zotero_write import ZoteroWriter

PDF = b"%PDF-1.7 a paper"


class _Bridge:
    """Stands in for the plugin; find_file writes `content` where it is told."""

    def __init__(self, answer=None, content=PDF, error=None):
        self.answer = answer if answer is not None else {
            "eligible": True, "found": True, "url": "https://publisher/x.pdf", "version": ""}
        self.content, self.error = content, error
        self.find_calls, self.imports, self.links = [], [], []

    def find_file(self, parent_key, path, methods=None, timeout=None):
        self.find_calls.append({"path": path, "methods": methods, "timeout": timeout})
        if self.error:
            raise self.error
        if self.answer.get("found"):
            with open(path, "wb") as f:
                f.write(self.content)
        return self.answer

    def import_file(self, parent_key, path):
        with open(path, "rb") as f:
            self.imports.append(f.read())
        return {"created": ["STORED01"]}

    def link_attachment(self, parent_key, path, title=""):
        self.links.append(path)
        return {"created": ["LINKED01"]}


class _Reader:
    def children(self, key):
        return []

    def item_raw(self, key):
        return {"key": key, "itemType": "journalArticle", "citationKey": "kiddlePaper2026"}


def _writer(tmp_path, bridge, linked=True, **zotero):
    cfg = Config(index_dir=tmp_path / "data")
    cfg.index_dir.mkdir(parents=True)
    base = tmp_path / "Papers"
    save_settings(cfg, {"zotero": {"linked_attachments_dir": str(base) if linked else "", **zotero}})
    woken = []
    w = ZoteroWriter(cfg, on_write=lambda: woken.append(1))
    w._bridge, w._reader = bridge, _Reader()
    return w, base, woken


def _leftovers(writer):
    return list(writer.config.index_dir.glob("find-pdf-*"))


def test_a_library_with_a_linked_folder_gets_a_linked_file_named_by_citation_key(tmp_path):
    bridge = _Bridge()
    w, base, woken = _writer(tmp_path, bridge)
    result = w.find_pdf("PARENT01")
    assert result["status"] == "attached" and result["attachment_key"] == "LINKED01"
    assert (base / "kiddlePaper2026.pdf").read_bytes() == PDF
    assert bridge.links == [str(base / "kiddlePaper2026.pdf")] and bridge.imports == []
    assert _leftovers(w) == [] and woken == [1]


def test_a_library_without_a_linked_folder_gets_a_stored_file(tmp_path):
    bridge = _Bridge()
    w, _, woken = _writer(tmp_path, bridge, linked=False)
    result = w.find_pdf("PARENT01")
    assert result["status"] == "attached" and result["attachment_key"] == "STORED01"
    assert bridge.imports == [PDF] and bridge.links == []
    assert _leftovers(w) == [] and woken == [1]


def test_every_zotero_source_is_used_with_the_timeout_from_the_settings(tmp_path):
    bridge = _Bridge()
    w, _, _ = _writer(tmp_path, bridge, find_timeout_seconds=42)
    w.find_pdf("PARENT01")
    assert bridge.find_calls[-1]["methods"] is None
    assert bridge.find_calls[-1]["timeout"] == 42


def test_an_earlier_version_is_refused_unless_the_setting_allows_it(tmp_path):
    for label in ("submittedVersion", "acceptedVersion", "submitted", "accepted"):
        answer = {"eligible": True, "found": True, "url": "https://arxiv.org/pdf/1", "version": label}
        bridge = _Bridge(answer)
        w, _, woken = _writer(tmp_path / label, bridge)
        result = w.find_pdf("PARENT01")
        assert result == {"status": "earlier_version_only", "version": label,
                          "url": "https://arxiv.org/pdf/1"}
        assert bridge.links == [] and woken == [] and _leftovers(w) == []
    answer = {"eligible": True, "found": True, "url": "https://arxiv.org/pdf/1",
              "version": "submittedVersion"}
    bridge = _Bridge(answer)
    w, _, _ = _writer(tmp_path / "allowed", bridge, find_accept_earlier_versions=1)
    assert w.find_pdf("PARENT01")["status"] == "attached"


def test_a_published_label_is_taken(tmp_path):
    bridge = _Bridge({"eligible": True, "found": True, "url": "u", "version": "publishedVersion"})
    w, _, _ = _writer(tmp_path, bridge)
    assert w.find_pdf("PARENT01")["status"] == "attached"


def test_an_ineligible_item_is_skipped_and_nothing_found_changes_nothing(tmp_path):
    for answer, status in (({"eligible": False}, "skipped"),
                           ({"eligible": True, "found": False}, "not_found")):
        bridge = _Bridge(answer)
        w, _, woken = _writer(tmp_path / status, bridge)
        assert w.find_pdf("PARENT01")["status"] == status
        assert bridge.links == [] and bridge.imports == [] and woken == []
        assert _leftovers(w) == []


def test_a_file_that_is_not_a_pdf_is_not_attached(tmp_path):
    bridge = _Bridge(content=b"PK\x03\x04 an epub")
    w, _, _ = _writer(tmp_path, bridge)
    result = w.find_pdf("PARENT01")
    assert result["status"] == "failed" and "not a PDF" in result["error"]
    assert bridge.links == [] and _leftovers(w) == []


def test_a_timeout_is_reported_for_that_item_and_cleans_up(tmp_path):
    bridge = _Bridge(error=BridgeTimeout("no answer"))
    w, _, _ = _writer(tmp_path, bridge)
    result = w.find_pdf("PARENT01")
    assert result == {"status": "failed", "error": "no answer within 90s; nothing attached"}
    assert _leftovers(w) == []


def test_concurrent_log_lines_stay_whole(tmp_path):
    w, _, _ = _writer(tmp_path, _Bridge())
    entry = {"source": "x" * 20_000}
    threads = [threading.Thread(target=lambda: [w._log(entry) for _ in range(40)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (w.config.index_dir / "writes.jsonl").read_text().splitlines()
    assert len(lines) == 320 and all(json.loads(line) == entry for line in lines)
