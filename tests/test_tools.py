"""Hub tools report failure as an MCP tool error, except that an attach
whose indexing fails still succeeds."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from scriptorium import daemon, tools
from scriptorium.zotero import ZoteroUnavailableError


def _call(name, args):
    async def go():
        async with Client(tools.mcp) as c:
            return (await c.call_tool(name, args)).data
    return asyncio.run(go())


@pytest.fixture
def fake(monkeypatch):
    def unavailable(*_):
        raise ZoteroUnavailableError()

    d = SimpleNamespace(
        hub=SimpleNamespace(catalogue_lookup=lambda key: None),
        reader=SimpleNamespace(children=unavailable, tags=unavailable),
        writer=SimpleNamespace(link_attachment=lambda *a, **k: {"created": ["ATT1"]}),
        write_lock=threading.Lock(), cfg=None,
        run_sync=lambda: {"error": "a sync or ingest is already running"},
    )
    monkeypatch.setattr(daemon, "_current", d)
    return d


def test_unknown_item_is_a_tool_error(fake):
    with pytest.raises(ToolError, match="no catalogued item"):
        _call("hub_item", {"key": "NOPE"})


def test_zotero_closed_is_a_tool_error(fake):
    with pytest.raises(ToolError):
        _call("hub_tags", {})


def test_sync_refusal_is_a_tool_error(fake):
    with pytest.raises(ToolError, match="already running"):
        _call("hub_sync", {})


def test_attach_survives_an_indexing_failure(fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("index locked")

    monkeypatch.setattr(tools, "ingest_items", boom)
    result = _call("hub_attachment_link", {"parent_key": "P", "pdf_path": "/x.pdf"})
    assert result["created"] == ["ATT1"]
    assert "attached, but indexing failed" in result["indexed"]["error"]


def test_attach_during_a_sync_defers_indexing(fake, monkeypatch):
    monkeypatch.setattr(tools, "LOCK_WAIT_S", 0.01)
    fake.write_lock.acquire()  # a sync is running
    try:
        result = _call("hub_attachment_link", {"parent_key": "P", "pdf_path": "/x.pdf"})
    finally:
        fake.write_lock.release()
    assert result["created"] == ["ATT1"]
    assert "next sync" in result["indexed"]["deferred"]


def test_index_during_a_sync_is_a_tool_error(fake, monkeypatch):
    monkeypatch.setattr(tools, "LOCK_WAIT_S", 0.01)
    fake.write_lock.acquire()
    try:
        with pytest.raises(ToolError, match="sync is running"):
            _call("hub_index", {"item_keys": ["P"]})
    finally:
        fake.write_lock.release()


def test_update_refuses_tags_and_collections(fake):
    fake.writer.update_item = lambda *a, **k: pytest.fail("the writer must not be called")
    for patch, named in (({"tags": [{"tag": "x"}]}, "tags"),
                         ({"collections": ["K"], "extra": "y"}, "collections")):
        with pytest.raises(ToolError, match=f"does not change {named}.*hub_item_tags"):
            _call("hub_item_update", {"item_key": "ITEM0001", "patch": patch})


def test_tag_tool_passes_add_and_remove_to_the_writer(fake):
    calls = []
    fake.writer.edit_tags = lambda keys, add, remove, source=None: calls.append(
        (keys, add, remove, source)) or {"updated": {}, "failed": {}}
    _call("hub_item_tags", {"item_keys": ["ITEM0001"], "add": ["x"], "run": "ingestion/t"})
    assert calls == [(["ITEM0001"], ["x"], [], "ingestion/t")]
