"""Note text shown by hub_children and hub_trash is plain text: tags
removed, HTML entities decoded."""

from types import SimpleNamespace

from scriptorium.config import Config
from scriptorium.zotero import ZoteroReader


def test_note_snippet_is_plain_text(tmp_path):
    reader = ZoteroReader(Config(index_dir=tmp_path))
    note = {"key": "NOTE0001", "itemType": "note", "dateAdded": "2026-10-20",
            "note": "<p>First &amp; second</p><p>with &lt;brackets&gt;</p>"}
    reader._zot = SimpleNamespace(count_items=lambda: 1,
                                  children=lambda key: [{"data": note}])
    (entry,) = reader.children("ITEM0001")
    assert entry["snippet"] == "First & second with <brackets>"


def test_trash_lists_items_and_notes_with_readable_titles(tmp_path):
    reader = ZoteroReader(Config(index_dir=tmp_path))
    trashed = [
        {"data": {"key": "ITEM0001", "itemType": "preprint", "title": "A preprint",
                  "dateModified": "2026-10-15"}},
        {"data": {"key": "NOTE0001", "itemType": "note", "parentItem": "ITEM0002",
                  "note": "<p>Notes &amp; more</p>", "dateModified": "2026-10-20"}},
    ]
    reader._zot = SimpleNamespace(count_items=lambda: 1, trash=lambda: trashed,
                                  everything=lambda result: result)
    assert reader.trash() == [  # most recently modified first
        {"key": "NOTE0001", "item_type": "note", "title": "Notes & more",
         "parent": "ITEM0002", "date_modified": "2026-10-20"},
        {"key": "ITEM0001", "item_type": "preprint", "title": "A preprint", "parent": "",
         "date_modified": "2026-10-15"},
    ]
