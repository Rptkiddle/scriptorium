"""hub_children's note snippets are plain text: tags removed, HTML entities
decoded."""

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
