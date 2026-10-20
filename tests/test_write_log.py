"""Every Zotero write is appended to <data>/writes.jsonl, failures included,
with the item's state before updates and deletes."""

import json

import pytest

from scriptorium.config import Config
from scriptorium.zotero_bridge import BridgeUnavailable
from scriptorium.zotero_write import WRITE_LOG, ZoteroWriter, ZoteroWriteError


class _FakeBridge:
    def update_item(self, key, patch):
        return {"key": key, "version": 2}

    def delete_item(self, key):
        return {"key": key}

    def create_items(self, items):
        raise BridgeUnavailable("connection refused")

    def rename_collection(self, key, name):
        return {"updated": key, "name": name}

    def delete_collection(self, key):
        return {"deleted": key}

    def move_collection(self, key, parent_key):
        return {"moved": key, "parentKey": parent_key}


class _FakeReader:
    def item_raw(self, key):
        return {"key": key, "title": "Old title"} if key != "GONE" else None

    def citation_keys(self):
        return set()

    # EMPTY001 empty; FULL0001 holds one paper; PARENT01 has a subcollection;
    # VENUE001 is an empty [✗] venue folder.
    COLLECTIONS = (
        {"key": "EMPTY001", "name": "Old &amp; unused", "parent": "TOP00001", "items": 0},
        {"key": "FULL0001", "name": "Reading", "parent": "", "items": 1},
        {"key": "PARENT01", "name": "Journals", "parent": "", "items": 0},
        {"key": "CHILD001", "name": "Child", "parent": "PARENT01", "items": 0},
        {"key": "VENUE001", "name": "[✗] Some Journal", "parent": "", "items": 0},
    )

    def collections(self):
        return list(self.COLLECTIONS)

    def collection_items(self, key):
        return [{"key": "PAPER001"}] if key == "FULL0001" else []


def _writer(tmp_path, source="daemon"):
    w = ZoteroWriter(Config(index_dir=tmp_path), source=source)
    w._bridge = _FakeBridge()
    w._reader = _FakeReader()
    return w


def _log(tmp_path):
    return [json.loads(line) for line in (tmp_path / WRITE_LOG).read_text().splitlines()]


def test_update_logs_request_response_and_before_state(tmp_path):
    w = _writer(tmp_path)
    w.update_item("ABCD1234", {"title": "New title"}, source="ingestion/2026-10-09-1500")
    (entry,) = _log(tmp_path)
    assert entry["source"] == "ingestion/2026-10-09-1500"
    assert entry["operation"] == "update of ABCD1234"
    assert entry["before"]["title"] == "Old title"
    assert entry["request"]["patch"] == {"title": "New title"}
    assert entry["response"] == {"key": "ABCD1234", "version": 2}


def test_failed_write_is_logged_and_raised(tmp_path):
    w = _writer(tmp_path, source="surveillance/2026-10-09-0200")
    with pytest.raises(ZoteroWriteError):
        w.create_items([{"itemType": "note", "note": "x"}])
    (entry,) = _log(tmp_path)
    assert entry["source"] == "surveillance/2026-10-09-0200"
    assert entry["error"].startswith("BridgeUnavailable")
    assert "response" not in entry


def test_log_is_append_only(tmp_path):
    w = _writer(tmp_path)
    w.delete_item("K1")
    w.delete_item("K2")
    assert [e["request"]["key"] for e in _log(tmp_path)] == ["K1", "K2"]
    assert all(e["source"] == "daemon" for e in _log(tmp_path))


def test_before_state_of_a_missing_item_is_recorded(tmp_path):
    w = _writer(tmp_path)
    w.delete_item("GONE")
    (entry,) = _log(tmp_path)
    assert entry["before"] == {"unavailable": "item not found"}


def test_writer_reads_through_the_given_reader(tmp_path):
    reader = _FakeReader()
    w = ZoteroWriter(Config(index_dir=tmp_path), reader=reader)
    assert w._reader is reader


def test_delete_refuses_collections_with_contents_and_venue_folders(tmp_path):
    w = _writer(tmp_path)
    for key, reason in (("FULL0001", "holds 1 items"), ("PARENT01", "1 subcollections"),
                        ("VENUE001", "venue folder")):
        with pytest.raises(ZoteroWriteError, match=reason):
            w.delete_collection(key)
    assert not (tmp_path / WRITE_LOG).exists()  # a refusal is not a write
    with pytest.raises(ZoteroWriteError, match="no collection"):
        w.delete_collection("MISSING1")


def test_delete_logs_what_is_needed_to_rebuild_the_collection(tmp_path):
    w = _writer(tmp_path)
    w.delete_collection("EMPTY001", source="ingestion/test")
    w.delete_collection("FULL0001", force=True)
    w.delete_collection("VENUE001", force=True)
    empty, full, venue = _log(tmp_path)
    assert empty["operation"] == "collection delete" and empty["source"] == "ingestion/test"
    assert empty["before"] == {"key": "EMPTY001", "name": "Old & unused", "parent": "TOP00001",
                               "subcollections": [], "item_count": 0, "items": []}
    assert full["before"]["items"] == ["PAPER001"] and full["request"]["force"] is True
    assert venue["response"] == {"deleted": "VENUE001"}


def test_rename_logs_the_old_name_and_refuses_an_empty_one(tmp_path):
    w = _writer(tmp_path)
    w.rename_collection("VENUE001", "[✓] Some Journal")
    (entry,) = _log(tmp_path)
    assert entry["operation"] == "collection rename"
    assert entry["before"]["name"] == "[✗] Some Journal"
    assert entry["request"] == {"key": "VENUE001", "name": "[✓] Some Journal"}
    with pytest.raises(ZoteroWriteError, match="empty"):
        w.rename_collection("VENUE001", "   ")


def test_move_logs_the_old_parent_and_allows_the_top_level(tmp_path):
    w = _writer(tmp_path)
    w.move_collection("EMPTY001", "PARENT01", source="ingestion/test")
    w.move_collection("CHILD001", "")
    to_parent, to_top = _log(tmp_path)
    assert to_parent["operation"] == "collection move"
    assert to_parent["before"]["parent"] == "TOP00001"
    assert to_parent["request"] == {"key": "EMPTY001", "parent_key": "PARENT01"}
    assert to_top["request"]["parent_key"] == "" and to_top["before"]["parent"] == "PARENT01"


def test_move_refuses_a_missing_parent_and_its_own_subtree(tmp_path):
    w = _writer(tmp_path)
    with pytest.raises(ZoteroWriteError, match="no collection"):
        w.move_collection("EMPTY001", "MISSING1")
    with pytest.raises(ZoteroWriteError, match="itself or its own"):
        w.move_collection("PARENT01", "PARENT01")
    with pytest.raises(ZoteroWriteError, match="itself or its own"):
        w.move_collection("PARENT01", "CHILD001")
    assert not (tmp_path / WRITE_LOG).exists()
