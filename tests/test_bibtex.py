"""hub_bibtex: Zotero's own export, keyed by citation key, without local
paths, library tags and (by default) abstracts."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from scriptorium import daemon, tools
from scriptorium.config import Config
from scriptorium.zotero import strip_bib_fields

ENTRY = """@inproceedings{smithTitle2025,
\ttitle = {A {Nested} {AAAI} Title},
\tabstract = {First line with \\{escaped\\} braces
second line},
\tbooktitle = {Proceedings of {ICWSM}},
\tyear = {2025},
\tnote = {arXiv: 2410.08642
SocArXiv: 10.31235/osf.io/rx4um},
\tkeywords = {backfill-2020+},
\tpages = {1--10},
\tfile = {smithTitle2025.pdf:/Users/someone/Papers/smithTitle2025.pdf:application/pdf},
}
"""


def test_strip_removes_whole_fields_including_multiline_values():
    out = strip_bib_fields(ENTRY, {"file", "keywords", "abstract"})
    assert "abstract" not in out and "second line" not in out
    assert "keywords" not in out and "/Users/" not in out
    assert "\tnote = {arXiv: 2410.08642\nSocArXiv: 10.31235/osf.io/rx4um},\n" in out
    assert "\ttitle = {A {Nested} {AAAI} Title},\n\tbooktitle" in out
    assert out.endswith("\tpages = {1--10},\n}\n")
    assert out.count("{") == out.count("}")


def test_strip_keeps_fields_not_named():
    assert strip_bib_fields(ENTRY, {"file"}).count("abstract = {First line") == 1


def _call(name, args):
    async def go():
        async with Client(tools.mcp) as c:
            return (await c.call_tool(name, args)).data
    return asyncio.run(go())


@pytest.fixture
def fake(monkeypatch, tmp_path):
    catalogue = {"smithTitle2025": {"item_key": "ITEM0001"}, "ITEM0002": {"item_key": "ITEM0002"}}
    exported = []

    def export_bib(keys, fmt):
        exported.append((keys, fmt))
        return ENTRY
    d = SimpleNamespace(hub=SimpleNamespace(catalogue_lookup=catalogue.get),
                        reader=SimpleNamespace(export_bib=export_bib),
                        write_lock=threading.Lock(), cfg=Config(index_dir=tmp_path))
    monkeypatch.setattr(daemon, "_current", d)
    return exported


def test_keys_resolve_and_missing_ones_are_listed(fake):
    result = _call("hub_bibtex", {"keys": ["smithTitle2025", "ITEM0002", "nope2020",
                                           "smithTitle2025"]})
    assert fake == [(["ITEM0001", "ITEM0002"], "biblatex")]  # default from settings
    assert result["missing"] == ["nope2020"] and result["format"] == "biblatex"
    assert result["entries"] == 1 and "file =" not in result["bibtex"]
    assert "abstract =" not in result["bibtex"]


def test_format_and_abstracts_options(fake):
    result = _call("hub_bibtex", {"keys": ["smithTitle2025"], "format": "bibtex",
                                  "abstracts": True})
    assert fake[-1][1] == "bibtex" and "abstract =" in result["bibtex"]
    with pytest.raises(ToolError, match="format must be one of"):
        _call("hub_bibtex", {"keys": ["smithTitle2025"], "format": "ris"})


def test_key_limits(fake):
    with pytest.raises(ToolError, match="1-50 keys"):
        _call("hub_bibtex", {"keys": [f"k{i}" for i in range(51)]})
    with pytest.raises(ToolError, match="1-15 keys"):
        _call("hub_bibtex", {"keys": [f"k{i}" for i in range(16)], "abstracts": True})
