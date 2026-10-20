"""Run folders: creation, manifest updates, finishing, and the arm INDEX.md."""

import datetime

import pytest

from scriptorium import runs
from scriptorium.config import Config


def _at(monkeypatch, minute):
    t = datetime.datetime(2026, 10, 8, 9, minute).astimezone()
    monkeypatch.setattr(runs, "_now", lambda: t)


def test_new_run_creates_work_folder_and_running_manifest(tmp_path, monkeypatch):
    _at(monkeypatch, 5)
    run_dir = runs.new_run(Config(index_dir=tmp_path), "interrogation", "agent-chat", "s1")
    assert run_dir == tmp_path / "interrogation" / "runs" / "2026-10-08-090500-agent-chat"
    assert (run_dir / "work").is_dir()
    m = runs.read_manifest(run_dir)
    assert m["arm"] == "interrogation" and m["status"] == "running"
    assert m["session"] == "s1" and m["finished"] is None


def test_new_run_refuses_an_existing_folder(tmp_path, monkeypatch):
    _at(monkeypatch, 5)
    config = Config(index_dir=tmp_path)
    runs.new_run(config, "retrieval")
    with pytest.raises(FileExistsError):
        runs.new_run(config, "retrieval")


def test_unknown_arm_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        runs.new_run(Config(index_dir=tmp_path), "retrieve")


def test_finish_records_status_and_rebuilds_index_newest_first(tmp_path, monkeypatch):
    config = Config(index_dir=tmp_path)
    _at(monkeypatch, 1)
    first = runs.new_run(config, "surveillance")
    _at(monkeypatch, 2)
    second = runs.new_run(config, "surveillance")
    (tmp_path / "surveillance" / "runs" / "2026-10-08-0903").mkdir()  # no run.json

    runs.finish(first, "ok", "3 candidates, 1 accepted")
    m = runs.finish(second, "partial", "2 candidates, 2 accepted", report={"accepted": 2})
    assert m["status"] == "partial" and m["finished"] and m["report"] == {"accepted": 2}

    index = (tmp_path / "surveillance" / "INDEX.md").read_text().splitlines()
    entries = [line for line in index if line.startswith("- ")]
    assert entries == [
        "- `2026-10-08-0903` — no run.json",
        "- `2026-10-08-090200` — partial — 2 candidates, 2 accepted",
        "- `2026-10-08-090100` — ok — 3 candidates, 1 accepted",
    ]


def test_finish_rejects_unknown_status(tmp_path):
    run_dir = runs.new_run(Config(index_dir=tmp_path), "discovery", "q")
    with pytest.raises(ValueError):
        runs.finish(run_dir, "done")


def test_later_finish_keeps_finished_and_appends_history(tmp_path, monkeypatch):
    _at(monkeypatch, 1)
    run_dir = runs.new_run(Config(index_dir=tmp_path), "surveillance")
    runs.finish(run_dir, "partial", "1 accepted, 0 added")
    _at(monkeypatch, 30)
    m = runs.finish(run_dir, "ok", "1 accepted, 1 added", by="2026-10-09-0200")
    assert m["finished"].startswith("2026-10-08T09:01")
    assert m["status"] == "ok" and m["summary"] == "1 accepted, 1 added"
    assert [(h["status"], h["by"]) for h in m["history"]] == [
        ("partial", "self"), ("ok", "2026-10-09-0200")]


def test_new_run_records_the_code_version(tmp_path):
    m = runs.read_manifest(runs.new_run(Config(index_dir=tmp_path), "ingestion"))
    assert len(m["provenance"]["commit"]) == 40 and "dirty" in m["provenance"]
