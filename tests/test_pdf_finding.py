"""Finding PDFs for many items: requests are spread over publishers, started
at the set pace, and never run beyond the set concurrency."""

import threading

from scriptorium import pdf_finding
from scriptorium.config import Config
from scriptorium.pdf_finding import count_statuses, find_pdfs, publisher_group, round_robin
from scriptorium.settings import save_settings


def _config(tmp_path, **zotero):
    cfg = Config(index_dir=tmp_path / "data")
    cfg.index_dir.mkdir(parents=True)
    save_settings(cfg, {"zotero": zotero})
    return cfg


class _Reader:
    def __init__(self, items):
        self.items = items

    def item_raw(self, key):
        return self.items.get(key)


class _Writer:
    def __init__(self, status="attached"):
        self.status, self.keys, self.lock = status, [], threading.Lock()

    def find_pdf(self, key, source=None):
        with self.lock:
            self.keys.append(key)
        return {"status": self.status}


def _items(*dois):
    return {f"K{i}": {"itemType": "journalArticle", "DOI": d} for i, d in enumerate(dois)}


def test_groups_come_from_the_doi_prefix_or_the_url_host():
    assert publisher_group({"DOI": "10.1177/2633"}) == "10.1177"
    assert publisher_group({"DOI": "", "url": "https://www.Example.org/a"}) == "www.example.org"
    assert publisher_group({}) == ""


def test_round_robin_takes_one_from_each_group_in_turn():
    groups = {"a1": "A", "a2": "A", "a3": "A", "b1": "B", "c1": "C", "c2": "C"}
    assert round_robin(groups) == ["a1", "b1", "c1", "a2", "c2", "a3"]


def test_requests_follow_round_robin_and_the_set_pace(tmp_path):
    cfg = _config(tmp_path, find_interval_seconds=2, find_concurrency=1)
    items = _items("10.1/a", "10.1/b", "10.2/c")
    now, sleeps, starts = [0.0], [], []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    writer = _Writer()
    original = writer.find_pdf

    def timed(key, source=None):
        starts.append(now[0])
        return original(key, source=source)

    writer.find_pdf = timed
    find_pdfs(cfg, list(items), writer=writer, reader=_Reader(items), sleep=sleep, clock=lambda: now[0])
    assert writer.keys == ["K0", "K2", "K1"]
    assert starts == [0.0, 2.0, 4.0] and sleeps == [2.0, 2.0]


def test_a_request_starts_without_waiting_for_earlier_ones_up_to_the_limit(tmp_path):
    cfg = _config(tmp_path, find_interval_seconds=0, find_concurrency=3)
    items = _items(*[f"10.{i}/x" for i in range(9)])
    release = threading.Event()
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()
    three_running = threading.Barrier(3, action=release.set)

    class Blocking(_Writer):
        def find_pdf(self, key, source=None):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            if not release.is_set():
                three_running.wait(timeout=5)  # all three must be running together
            with lock:
                state["now"] -= 1
            return super().find_pdf(key, source)

    writer = Blocking()
    results = find_pdfs(cfg, list(items), writer=writer, reader=_Reader(items))
    assert state["peak"] == 3 and len(writer.keys) == 9 and len(results) == 9


def test_preprints_are_skipped_unless_the_setting_includes_them(tmp_path):
    items = {"P": {"itemType": "preprint", "DOI": "10.1/x"}}
    writer = _Writer()
    results = find_pdfs(_config(tmp_path / "off"), ["P"], writer=writer, reader=_Reader(items))
    assert results["P"]["status"] == "skipped" and writer.keys == []
    results = find_pdfs(_config(tmp_path / "on", find_include_preprints=1), ["P"],
                        writer=writer, reader=_Reader(items))
    assert results["P"]["status"] == "attached"


def test_an_unknown_item_or_a_crash_is_reported_and_the_batch_carries_on(tmp_path):
    cfg = _config(tmp_path, find_interval_seconds=0)
    items = _items("10.1/a", "10.2/b")

    class Crashes(_Writer):
        def find_pdf(self, key, source=None):
            if key == "K0":
                raise RuntimeError("boom")
            return super().find_pdf(key, source)

    seen = []
    results = find_pdfs(cfg, ["K0", "NOPE", "K1", "K1"], writer=Crashes(), reader=_Reader(items),
                        on_result=lambda k, r: seen.append(k))
    assert list(results) == ["K0", "NOPE", "K1"]
    assert results["K0"]["status"] == "failed" and "boom" in results["K0"]["error"]
    assert results["NOPE"]["status"] == "failed" and results["K1"]["status"] == "attached"
    assert sorted(seen) == ["K0", "K1", "NOPE"]
    assert count_statuses(results) == {"failed": 2, "attached": 1}


def test_the_script_reads_keys_from_an_added_file(tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "find_pdfs_script", Path(pdf_finding.__file__).parents[2] / "scripts" / "find_pdfs.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    added = tmp_path / "added.json"
    added.write_text('{"created": {"10.1/a": {"key": "K2"}, "10.1/b": {"key": "K3"}}}')
    assert script.named_keys(["K1", "K2"], str(added)) == ["K1", "K2", "K3"]
