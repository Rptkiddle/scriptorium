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

    def edit_tags(self, keys, add, remove):
        return {"updated": {k: ["kept", *add] for k in keys}, "failed": {}}

    def edit_collections(self, keys, add, remove):
        return {"updated": {k: ["COLL0001", *add] for k in keys}, "failed": {}}

    def create_note(self, parent_key, note):
        return {"created": "NOTE0001", "parentKey": parent_key}

    def change_type(self, keys, item_type, dry_run):
        return {"updated": {k: {"itemType": item_type, "movedToExtra": {"ISSN": "1"}}
                            for k in keys}, "failed": {}, "dryRun": dry_run}

    def restore_items(self, keys):
        return {"restored": keys, "failed": {}}


class _FakeReader:
    def item_raw(self, key):
        if key == "GONE":
            return None
        return {"key": key, "title": "Old title", "tags": [{"tag": "kept"}],
                "collections": ["COLL0001"]}

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


def test_tag_edit_logs_each_items_tags_before(tmp_path):
    w = _writer(tmp_path)
    result = w.edit_tags(["ITEM0001", "GONE"], add=["new &amp; shiny "], source="ingestion/t")
    assert result["updated"]["ITEM0001"] == ["kept", "new & shiny"]
    (entry,) = _log(tmp_path)
    assert entry["operation"] == "tag edit" and entry["source"] == "ingestion/t"
    assert entry["request"] == {"keys": ["ITEM0001", "GONE"], "add": ["new & shiny"],
                                "remove": []}
    assert entry["before"] == {"ITEM0001": [{"tag": "kept"}],
                               "GONE": {"unavailable": "item not found"}}


def test_collection_edit_logs_each_items_collections_before(tmp_path):
    w = _writer(tmp_path)
    w.edit_collections(["ITEM0001"], add=["COLL0002"], remove=["COLL0003"])
    (entry,) = _log(tmp_path)
    assert entry["operation"] == "collection edit"
    assert entry["request"] == {"keys": ["ITEM0001"], "add": ["COLL0002"],
                                "remove": ["COLL0003"]}
    assert entry["before"] == {"ITEM0001": ["COLL0001"]}


@pytest.mark.parametrize("keys, add, remove, message", [
    ("ITEM0001", ["x"], [], "as a list"),
    (["ITEM0001"], "x", [], "as a list"),
    ([], ["x"], [], "at least one item key"),
    (["ITEM0001"], [], [], "to add or remove"),
    (["ITEM0001"], ["x"], ["x"], "both added and removed: x"),
    (["ITEM0001"], [" "], [], "non-empty string"),
])
def test_list_edits_refuse_bad_arguments_without_writing(tmp_path, keys, add, remove, message):
    w = _writer(tmp_path)
    with pytest.raises(ZoteroWriteError, match=message):
        w.edit_tags(keys, add, remove)
    assert not (tmp_path / WRITE_LOG).exists()


def test_note_text_becomes_html_paragraphs(tmp_path):
    w = _writer(tmp_path)
    w.create_note("ITEM0001", "First line\nsecond <line>\n\nNext paragraph")
    w.create_note("ITEM0001", "<p>Already <b>HTML</b></p>")
    plain, given = _log(tmp_path)
    assert plain["operation"] == "note create"
    assert plain["request"]["note"] == (
        "<p>First line<br>second &lt;line&gt;</p><p>Next paragraph</p>")
    assert given["request"]["note"] == "<p>Already <b>HTML</b></p>"
    with pytest.raises(ZoteroWriteError, match="cannot be empty"):
        w.create_note("ITEM0001", "  ")


def test_type_change_logs_full_before_state_and_dry_run_reads_nothing(tmp_path):
    w = _writer(tmp_path)
    w.change_type(["ITEM0001"], "conferencePaper", dry_run=True)
    w.change_type(["ITEM0001"], " conferencePaper ", source="ingestion/t")
    dry, real = _log(tmp_path)
    assert dry["operation"] == "type change (dry run)" and dry["before"] is None
    assert real["operation"] == "type change" and real["source"] == "ingestion/t"
    assert real["request"] == {"keys": ["ITEM0001"], "item_type": "conferencePaper",
                               "dry_run": False}
    assert real["before"]["ITEM0001"]["title"] == "Old title"
    with pytest.raises(ZoteroWriteError, match="new item type"):
        w.change_type(["ITEM0001"], " ")


def test_restore_is_logged(tmp_path):
    w = _writer(tmp_path)
    assert w.restore_items(["ITEM0001"])["restored"] == ["ITEM0001"]
    (entry,) = _log(tmp_path)
    assert entry["operation"] == "restore" and entry["request"] == {"keys": ["ITEM0001"]}
    with pytest.raises(ZoteroWriteError, match="at least one item key"):
        w.restore_items([])
