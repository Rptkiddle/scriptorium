"""Run folders: creation, manifest updates, finishing, and the kind's INDEX.md."""

import datetime

import pytest

from scriptorium_server import jobs


def _at(monkeypatch, minute):
    t = datetime.datetime(2026, 10, 8, 9, minute).astimezone()
    monkeypatch.setattr(jobs, "_now", lambda: t)


def test_new_run_creates_work_folder_and_running_manifest(tmp_path, monkeypatch):
    _at(monkeypatch, 5)
    run_dir = jobs.new_run(tmp_path, "fulltext", "one-pdf")
    assert run_dir == tmp_path / "fulltext" / "2026-10-08-090500-one-pdf"
    assert (run_dir / "work").is_dir()
    m = jobs.read_manifest(run_dir)
    assert m["kind"] == "fulltext" and m["status"] == "running" and m["finished"] is None


def test_new_run_refuses_an_existing_folder(tmp_path, monkeypatch):
    _at(monkeypatch, 5)
    jobs.new_run(tmp_path, "intake")
    with pytest.raises(FileExistsError):
        jobs.new_run(tmp_path, "intake")


@pytest.mark.parametrize("kind", ["", "a/b", "../up", ".hidden"])
def test_a_kind_that_is_not_a_folder_name_is_rejected(tmp_path, kind):
    with pytest.raises(ValueError):
        jobs.new_run(tmp_path, kind)


def test_finish_records_status_and_rebuilds_index_newest_first(tmp_path, monkeypatch):
    _at(monkeypatch, 1)
    first = jobs.new_run(tmp_path, "intake")
    _at(monkeypatch, 2)
    second = jobs.new_run(tmp_path, "intake")
    (tmp_path / "intake" / "2026-10-08-0903").mkdir()  # no run.json

    jobs.finish_run(first, "ok", "3 harvested, 1 admitted")
    m = jobs.finish_run(second, "partial", "2 harvested, 2 admitted", report={"admitted": 2})
    assert m["status"] == "partial" and m["finished"] and m["report"] == {"admitted": 2}

    index = (tmp_path / "intake" / "INDEX.md").read_text().splitlines()
    entries = [line for line in index if line.startswith("- ")]
    assert entries == [
        "- `2026-10-08-0903` — no run.json",
        "- `2026-10-08-090200` — partial — 2 harvested, 2 admitted",
        "- `2026-10-08-090100` — ok — 3 harvested, 1 admitted",
    ]


def test_finish_rejects_unknown_status(tmp_path):
    run_dir = jobs.new_run(tmp_path, "intake", "q")
    with pytest.raises(ValueError):
        jobs.finish_run(run_dir, "done")


def test_later_finish_keeps_finished_and_appends_history(tmp_path, monkeypatch):
    _at(monkeypatch, 1)
    run_dir = jobs.new_run(tmp_path, "intake")
    jobs.finish_run(run_dir, "partial", "1 harvested, 0 admitted")
    _at(monkeypatch, 30)
    m = jobs.finish_run(run_dir, "ok", "1 harvested, 1 admitted", by="2026-10-09-0200")
    assert m["finished"].startswith("2026-10-08T09:01")
    assert m["status"] == "ok" and m["summary"] == "1 harvested, 1 admitted"
    assert [(h["status"], h["by"]) for h in m["history"]] == [
        ("partial", "self"), ("ok", "2026-10-09-0200")]


def test_new_run_records_the_code_version(tmp_path):
    m = jobs.read_manifest(jobs.new_run(tmp_path, "intake"))
    assert len(m["provenance"]["commit"]) == 40 and "dirty" in m["provenance"]


def test_a_built_image_records_the_commit_it_was_built_from(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIPTORIUM_COMMIT", "abc123")
    m = jobs.read_manifest(jobs.new_run(tmp_path, "intake"))
    assert m["provenance"] == {"commit": "abc123", "dirty": False, "uncommitted": []}
