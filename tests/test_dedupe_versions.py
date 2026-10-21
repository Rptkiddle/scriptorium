"""dedupe_versions.py: a plan that changes nothing, then an apply that re-checks."""

import importlib.util
import json
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "dedupe_versions.py"
    spec = importlib.util.spec_from_file_location("dedupe_versions_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(key, item_type, doi="", **fields):
    return {"key": key, "itemType": item_type, "title": "Echo Chambers Revisited", "DOI": doi,
            "creators": [{"creatorType": "author", "lastName": "Trilling"}], **fields}


LIBRARY = {
    "PUB1": _item("PUB1", "journalArticle", "10.1000/journal.1"),
    "PRE1": _item("PRE1", "preprint", "10.31235/osf.io/abcde", repository="SocArXiv"),
}


class FakeReader:
    def __init__(self, config):
        self.items = {k: dict(v) for k, v in LIBRARY.items()}

    def library_snapshot(self):
        return list(self.items.values())

    def children(self, key):
        return []

    def item_raw(self, key):
        return self.items.get(key)


class FakeWriter:
    calls: list = []  # noqa: RUF012 — shared record of the calls

    def __init__(self, config, source="", reader=None):
        pass

    def update_item(self, key, patch):
        FakeWriter.calls.append(("update", key, patch))

    def delete_item(self, key):
        FakeWriter.calls.append(("delete", key))


def test_plan_changes_nothing_and_apply_records_then_trashes(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIPTORIUM_INDEX_DIR", str(tmp_path))
    module = _load()
    monkeypatch.setattr(module, "ZoteroReader", FakeReader)
    monkeypatch.setattr(module, "ZoteroWriter", FakeWriter)
    FakeWriter.calls = []
    config = module.load_config()
    run_dir = module.plan(config, FakeReader(config))
    assert FakeWriter.calls == []
    (row,) = json.loads((run_dir / "work" / "plan.json").read_text())
    assert (row["keep"], row["drop"], row["action"]) == ("PUB1", "PRE1", "recorded; trashed")

    module.apply(config, FakeReader(config), run_dir)
    assert FakeWriter.calls == [("update", "PUB1", {"extra": "SocArXiv: 10.31235/osf.io/abcde"}),
                                ("delete", "PRE1")]


def test_apply_skips_a_pair_whose_item_has_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIPTORIUM_INDEX_DIR", str(tmp_path))
    module = _load()
    FakeWriter.calls = []
    monkeypatch.setattr(module, "ZoteroWriter", FakeWriter)
    config = module.load_config()
    run_dir = module.plan(config, FakeReader(config))
    gone = FakeReader(config)
    gone.items["PRE1"]["deleted"] = True
    module.apply(config, gone, run_dir)
    assert FakeWriter.calls == []
    (line,) = (run_dir / "work" / "applied.jsonl").read_text().splitlines()
    assert json.loads(line)["outcome"].startswith("skipped")
