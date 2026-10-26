"""Menubar status summary: icon colour and lines for each daemon/Zotero state."""

from scriptorium.menubar import summarize

HEALTH = {"papers": 10, "chunks": 100, "catalogue_items": 20, "failed": 0,
          "library_version": 7}
BRIDGE = {"bridge": "0.1.4", "zotero": "10.0.5"}


def test_daemon_stopped_is_red():
    assert summarize(None, 7, BRIDGE)[0] == "red"


def test_zotero_closed_is_red_and_says_closed():
    # A closed Zotero also silences the Bridge; "closed" is the useful message.
    color, _, index = summarize(HEALTH, None, None)
    assert color == "red" and "Zotero closed" in index


def test_bridge_missing_while_zotero_open_is_red():
    color, _, index = summarize(HEALTH, 7, None)
    assert color == "red" and "Bridge not loaded" in index


def test_all_up_and_current_is_green():
    assert summarize(HEALTH, 7, BRIDGE)[0] == "green"


def test_failed_items_leave_the_icon_green_and_are_counted():
    color, _, index = summarize({**HEALTH, "failed": 17}, 7, BRIDGE)
    assert color == "green" and "up to date" in index and "17 failed item(s)" in index


def test_index_behind_zotero_is_amber():
    color, _, index = summarize(HEALTH, 9, BRIDGE)
    assert color == "amber" and "behind Zotero" in index


def test_zotero_closed_outranks_a_failed_sync():
    health = {**HEALTH, "last_sync_error": {"error": "connection refused"}}
    color, _, index = summarize(health, None, None)
    assert color == "red" and "Zotero closed" in index


def test_failed_sync_with_zotero_open_is_amber():
    health = {**HEALTH, "last_sync_error": {"error": "boom"}}
    color, _, index = summarize(health, 7, BRIDGE)
    assert color == "amber" and "failing" in index
