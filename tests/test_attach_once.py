"""Attaching a PDF the item already holds writes nothing and returns the
existing attachment; a different PDF still attaches."""

from scriptorium.config import Config
from scriptorium.settings import save_settings
from scriptorium.zotero_write import ZoteroWriter


class _Bridge:
    def __init__(self):
        self.calls = []

    def link_attachment(self, parent_key, path, title=""):
        self.calls.append(path)
        return {"created": ["NEWATT01"]}


class _Reader:
    def __init__(self, children, trashed=()):
        self._children, self._trashed = children, set(trashed)

    def children(self, key):
        return self._children

    def item_raw(self, key):
        return {"key": key, "citationKey": "kiddlePaper2026",
                **({"deleted": 1} if key in self._trashed else {})}


def _writer(tmp_path, children, trashed=()):
    cfg = Config(index_dir=tmp_path / "data")
    cfg.index_dir.mkdir(parents=True)
    base = tmp_path / "Papers"
    save_settings(cfg, {"zotero": {"linked_attachments_dir": str(base)}})
    w = ZoteroWriter(cfg)
    w._bridge, w._reader = _Bridge(), _Reader(children, trashed)
    return w, base


def _att(key, path):
    return {"key": key, "item_type": "attachment", "link_mode": "linked_file", "path": str(path)}


def test_the_same_pdf_again_returns_the_existing_attachment(tmp_path):
    held = tmp_path / "held.pdf"
    held.write_bytes(b"%PDF-1 same")
    inbox = tmp_path / "inbox-name.pdf"
    inbox.write_bytes(b"%PDF-1 same")
    w, base = _writer(tmp_path, [_att("ATT00001", held)])
    result = w.link_attachment("PARENT01", str(inbox))
    assert result == {"existing": True, "attachment_key": "ATT00001", "file": str(held)}
    assert w._bridge.calls == [] and not base.exists()


def test_a_different_or_trashed_pdf_attaches(tmp_path):
    held = tmp_path / "held.pdf"
    held.write_bytes(b"%PDF-1 old version")
    inbox = tmp_path / "new.pdf"
    inbox.write_bytes(b"%PDF-1 new version")
    w, base = _writer(tmp_path, [_att("ATT00001", held)])
    assert w.link_attachment("PARENT01", str(inbox))["created"] == ["NEWATT01"]
    assert (base / "kiddlePaper2026.pdf").read_bytes() == b"%PDF-1 new version"

    same = tmp_path / "same.pdf"
    same.write_bytes(b"%PDF-1 old version")
    w, _ = _writer(tmp_path / "t", [_att("ATT00001", held)], trashed={"ATT00001"})
    assert w.link_attachment("PARENT01", str(same))["created"] == ["NEWATT01"]
