"""Hub data safety: reader locking, PDF choice, removal guard, atomic writes."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from scriptorium.config import Config
from scriptorium.index import SyncState
from scriptorium.service import sync_library
from scriptorium.settings import SettingsFileError, load_settings, save_settings
from scriptorium.zotero import ZoteroReader


# -- reader lock ---------------------------------------------------------------

class _CountingZot:
    """Fake pyzotero client that records how many calls overlap."""

    def __init__(self):
        self.inside = 0
        self.max_inside = 0
        self._guard = threading.Lock()

    def _enter(self):
        with self._guard:
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)
        time.sleep(0.01)
        with self._guard:
            self.inside -= 1

    def count_items(self):
        self._enter()
        return 1

    def last_modified_version(self):
        self._enter()
        return 5


def test_reader_serialises_client_calls():
    reader = object.__new__(ZoteroReader)
    reader._zot = _CountingZot()
    reader._lock = threading.RLock()
    threads = [threading.Thread(target=reader.library_version) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert reader._zot.max_inside == 1


# -- PDF choice ----------------------------------------------------------------

def _reader_with_files(tmp_path, present: set[str]):
    reader = object.__new__(ZoteroReader)
    reader.config = SimpleNamespace(zotero_data_dir=tmp_path)
    for key in present:
        (tmp_path / "storage" / key).mkdir(parents=True)
        (tmp_path / "storage" / key / "f.pdf").write_bytes(b"%PDF")
    return reader


def _att(key, added):
    return {"key": key, "dateAdded": added, "linkMode": "imported_file",
            "filename": "f.pdf", "contentType": "application/pdf"}


def test_choose_pdf_skips_broken_earliest(tmp_path):
    reader = _reader_with_files(tmp_path, present={"NEW"})
    chosen = reader._choose_pdf([_att("OLD", "2020-01-01"), _att("NEW", "2024-01-01")])
    assert chosen["key"] == "NEW"


def test_choose_pdf_ties_break_on_key(tmp_path):
    reader = _reader_with_files(tmp_path, present={"AAA", "BBB"})
    chosen = reader._choose_pdf([_att("BBB", "2020-01-01"), _att("AAA", "2020-01-01")])
    assert chosen["key"] == "AAA"


def test_choose_pdf_none_resolve_returns_earliest(tmp_path):
    reader = _reader_with_files(tmp_path, present=set())
    chosen = reader._choose_pdf([_att("B", "2021-01-01"), _att("A", "2020-01-01")])
    assert chosen["key"] == "A"


# -- removal guard -------------------------------------------------------------

def _record(key, status):
    return SimpleNamespace(item_key=key, version=1, pdf_status=status,
                           citation_key=key, date_modified="")


class _FakeReader:
    def __init__(self, records):
        self.records = records

    def library_version(self):
        return 1

    def collections(self):
        return []

    def iter_papers(self):
        return self.records

    def all_item_keys(self):
        return {r.item_key for r in self.records}


class _FakeHub:
    def __init__(self, tmp_path, tracked):
        self.state = SyncState(
            library_version=1,
            items={k: {"version": 1} for k in tracked},
            failed={},
            catalogue={k: 1 for k in tracked},
            catalogue_meta=__import__("scriptorium.zotero").zotero.METADATA_VERSION,
            catalogue_doc=__import__("scriptorium.index").index.CATALOGUE_DOC_VERSION,
            catalogue_payload=__import__("scriptorium.index").index.CATALOGUE_PAYLOAD_VERSION,
            catalogue_folders=__import__("scriptorium.index").index.folders_signature({}),
            catalogue_pdf={k: True for k in tracked},
        )
        self.state_path = tmp_path / "state.json"
        self.deleted: list[str] = []

    def upsert_catalogue(self, records, folders=None):
        return len(records)

    def save_state(self):
        pass

    def delete_paper(self, key):
        self.deleted.append(key)
        self.state.items.pop(key, None)

    def delete_catalogue(self, key):
        pass


def test_sync_keeps_chunks_when_pdf_file_unavailable(tmp_path):
    records = [_record("MISSING", "linked_missing"), _record("EVICTED", "not_downloaded")]
    hub = _FakeHub(tmp_path, tracked={"MISSING", "EVICTED"})
    summary = sync_library(_FakeReader(records), hub, Config(index_dir=tmp_path))
    assert hub.deleted == []
    assert summary["unavailable"] == 2 and summary["removed"] == 0


def test_sync_removes_chunks_when_attachment_removed(tmp_path):
    hub = _FakeHub(tmp_path, tracked={"GONE"})
    summary = sync_library(_FakeReader([_record("GONE", "no_pdf")]), hub,
                           Config(index_dir=tmp_path))
    assert hub.deleted == ["GONE"] and summary["removed"] == 1


# -- atomic writes -------------------------------------------------------------

def test_state_save_is_atomic_and_leaves_no_temp(tmp_path):
    path = tmp_path / "state.json"
    state = SyncState(library_version=3, items={}, failed={}, catalogue={},
                      catalogue_pdf={"A": True})
    state.save(path)
    assert SyncState.load(path).catalogue_pdf == {"A": True}
    assert not (tmp_path / "state.json.tmp").exists()


def test_settings_save_refuses_to_overwrite_corrupt_file(tmp_path):
    config = Config(index_dir=tmp_path)
    path = tmp_path / "settings.json"
    path.write_text('{"discovery": {"s2_api_key": "KEEP')  # truncated
    with pytest.raises(SettingsFileError):
        save_settings(config, {"general": {"auto_sync": 1}})
    assert path.read_text() == '{"discovery": {"s2_api_key": "KEEP'
    assert load_settings(config)["general"]["auto_sync"] in (0, 1)  # still loads


def test_settings_new_file_is_private(tmp_path):
    config = Config(index_dir=tmp_path)
    save_settings(config, {"general": {"auto_sync": 1}})
    assert (tmp_path / "settings.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "settings.json").read_text())["general"]["auto_sync"] == 1


@pytest.mark.parametrize("incoming, message", [
    ({"discovery": {"model_arbiter": "opus"}}, "must differ"),
    ({"zotero": {"linked_attachments_dir": 5}}, "must be text"),
    ({"general": {"auto_sync_interval": float("inf")}}, "whole number"),
    (["not", "a", "dict"], "JSON object"),
])
def test_settings_validation(tmp_path, incoming, message):
    with pytest.raises(ValueError, match=message):
        save_settings(Config(index_dir=tmp_path), incoming)


# -- failed items are not re-parsed until the PDF or parser changes ----------

def test_sync_skips_a_failed_item_until_its_pdf_changes(tmp_path, monkeypatch):
    from scriptorium import service
    from scriptorium.parsing import PARSER_VERSION

    hub = _FakeHub(tmp_path, tracked=set())
    hub.state.failed = {"SCAN": "PDF parsed to empty text"}
    hub.state.failed_hash = {"SCAN": f"h1:{PARSER_VERSION}"}
    hub.state.catalogue = {"SCAN": 1}
    hub.state.catalogue_pdf = {"SCAN": True}
    parsed = []
    class _Hashes:
        def __init__(self, path):
            pass

        def of(self, r):
            return r.hash

        def save(self, prune=False):
            pass

    monkeypatch.setattr(service, "PdfHashes", _Hashes)
    monkeypatch.setattr(service, "parse_paper",
                        lambda r, c, h=None: parsed.append(r.item_key) or [])
    hub.classify_change = lambda r, h: "embed"
    hub.add_paper = lambda r, chunks, h: 0

    rec = SimpleNamespace(item_key="SCAN", version=1, pdf_status="ok",
                          citation_key="SCAN", date_modified="", hash="h1")
    summary = sync_library(_FakeReader([rec]), hub, Config(index_dir=tmp_path))
    assert parsed == [] and summary["failed"] == 1

    rec.hash = "h2"  # a new PDF: try again
    sync_library(_FakeReader([rec]), hub, Config(index_dir=tmp_path))
    assert parsed == ["SCAN"]


def test_a_renamed_folder_refreshes_the_stored_folder_fields(tmp_path):
    from scriptorium.index import folder_paths, folders_signature

    old = [{"key": "CAT", "name": "Proceedings", "parent": ""},
           {"key": "PUB", "name": "AAAI", "parent": "CAT"},
           {"key": "VEN", "name": "[✓] Web and Social Media", "parent": "PUB"}]
    new = [*old[:2], {"key": "VEN", "name": "[✓] Web and Social Media (ICWSM)", "parent": "PUB"}]

    class _Reader(_FakeReader):
        def __init__(self, records, colls):
            super().__init__(records)
            self.colls = colls

        def collections(self):
            return self.colls

    hub = _FakeHub(tmp_path, tracked=set())
    hub.state.catalogue = {"A": 1}
    hub.state.catalogue_pdf = {"A": False}
    hub.state.catalogue_folders = folders_signature(folder_paths(old))
    refreshed = []
    hub.refresh_catalogue_payloads = lambda recs, folders: refreshed.append(
        (sorted(r.item_key for r in recs), folders["VEN"][2])) or len(recs)
    rec = SimpleNamespace(item_key="A", version=1, pdf_status="no_pdf", citation_key="a",
                          date_modified="")
    sync_library(_Reader([rec], old), hub, Config(index_dir=tmp_path))
    assert refreshed == []  # folders unchanged: nothing to rewrite
    sync_library(_Reader([rec], new), hub, Config(index_dir=tmp_path))
    assert refreshed == [(["A"], "Web and Social Media (ICWSM)")]
    assert hub.state.catalogue_folders == folders_signature(folder_paths(new))
