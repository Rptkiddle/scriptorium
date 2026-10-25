"""Metadata derivation: venue field mapping and the METADATA_VERSION stamp."""

from types import SimpleNamespace

import pytest

from scriptorium.config import Config
from scriptorium.index import HubIndex, SyncState
from scriptorium.parsing import PARSER_VERSION
from scriptorium.zotero import METADATA_VERSION, _venue


@pytest.mark.parametrize("data, expected", [
    ({"itemType": "journalArticle", "publicationTitle": "J"}, "J"),
    ({"itemType": "presentation", "meetingName": "ICA 2026", "place": "Cape Town"}, "ICA 2026"),
    ({"itemType": "conferencePaper", "conferenceName": "CHI"}, "CHI"),
    ({"itemType": "conferencePaper", "proceedingsTitle": "Proc. CHI",
      "conferenceName": "CHI"}, "Proc. CHI"),
    ({"itemType": "report", "institution": "Reuters Institute"}, "Reuters Institute"),
    ({"itemType": "webpage", "websiteTitle": "Nieman Lab"}, "Nieman Lab"),
    ({"itemType": "thesis", "university": "VU Amsterdam"}, "VU Amsterdam"),
    ({"itemType": "bookSection", "bookTitle": "Handbook", "publisher": "Routledge"}, "Handbook"),
    ({"itemType": "preprint", "repository": "arXiv"}, "arXiv"),
    ({"itemType": "document"}, ""),
])
def test_venue_mapping(data, expected):
    assert _venue(data) == expected


def test_new_index_starts_without_stamps_and_keeps_them(tmp_path):
    cfg = Config(index_dir=tmp_path, embedding_dim=4)
    hub = HubIndex(cfg, embedder=object())
    assert hub.state.catalogue_meta == 0 and hub.state.library_version == 0
    hub.state.catalogue_meta = METADATA_VERSION
    hub.state.library_version = 5
    hub.save_state()
    hub.close()
    again = HubIndex(cfg, embedder=object()).state
    assert again.catalogue_meta == METADATA_VERSION and again.library_version == 5


def _hub(known: dict) -> HubIndex:
    hub = object.__new__(HubIndex)  # classify_change reads only the state
    hub.state = SyncState(library_version=0, items={"K": known}, failed={}, catalogue={})
    return hub


RECORD = SimpleNamespace(item_key="K", version=7)


def test_classify_unchanged_with_current_stamp():
    hub = _hub({"version": 7, "content_hash": "h", "parser": PARSER_VERSION,
                "meta": METADATA_VERSION})
    assert hub.classify_change(RECORD, "h") == "none"


def test_classify_missing_stamp_needs_metadata_refresh():
    hub = _hub({"version": 7, "content_hash": "h", "parser": PARSER_VERSION})
    assert hub.classify_change(RECORD, "h") == "metadata"


def test_classify_content_change_still_reembeds():
    hub = _hub({"version": 7, "content_hash": "old", "parser": PARSER_VERSION,
                "meta": METADATA_VERSION})
    assert hub.classify_change(RECORD, "new") == "embed"


# -- Zotero version-numbering reset (Zotero 10 local versions) --------------

from scriptorium.service import rebase_versions  # noqa: E402

LAST_SYNC = "2026-09-25T02:17:00Z"


def _rec(key, version=0, modified="2026-09-20T10:00:00Z", pdf="ok"):
    return SimpleNamespace(item_key=key, version=version, date_modified=modified,
                           pdf_status=pdf)


def _state():
    return SyncState(
        library_version=43366,
        items={"A": {"version": 43000}, "B": {"version": 43100}},
        failed={},
        catalogue={"A": 43000, "B": 43100, "C": 42000, "D": 42100},
    )


def test_no_reset_when_version_moves_forward():
    state = _state()
    assert rebase_versions(state, [_rec("A", 43400)], 43400, LAST_SYNC) is None
    assert state.items["A"]["version"] == 43000


def test_reset_rebases_unchanged_items():
    state = _state()
    stale = rebase_versions(state, [_rec("A"), _rec("C", pdf="no_pdf")], 0, LAST_SYNC)
    assert stale == []
    assert state.items["A"]["version"] == 0
    assert state.catalogue == {"A": 0, "B": 43100, "C": 0, "D": 42100}
    assert state.library_version == 0


def test_reset_leaves_items_edited_after_last_sync_stale():
    state = _state()
    stale = rebase_versions(state, [_rec("B", modified="2026-09-25T20:00:00Z")], 0, LAST_SYNC)
    assert stale == ["B"]
    assert state.items["B"]["version"] == 43100
    assert state.catalogue["B"] == 43100


def test_reset_leaves_catalogue_stale_when_pdf_presence_changed():
    state = _state()
    # D gained a PDF that the full-text index does not yet hold.
    stale = rebase_versions(state, [_rec("D", pdf="ok")], 0, LAST_SYNC)
    assert stale == ["D"]
    assert state.catalogue["D"] == 42100


def test_reset_without_known_last_sync_processes_everything():
    state = _state()
    stale = rebase_versions(state, [_rec("A"), _rec("C", pdf="no_pdf")], 0, "")
    assert stale == ["A", "C"]
