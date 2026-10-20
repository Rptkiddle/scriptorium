"""ingest_add.py keeps every add in an ingestion run."""

import importlib.util
import json
import sys
from pathlib import Path

from scriptorium.runs import read_manifest


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "ingest_add.py"
    spec = importlib.util.spec_from_file_location("ingest_add_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(tmp_path, monkeypatch, *extra):
    monkeypatch.setenv("SCRIPTORIUM_INDEX_DIR", str(tmp_path))
    entries = tmp_path / "in.json"
    entries.write_text(json.dumps([{"doi": "10.1/a"}]))
    module = _load()
    sources = []

    def fake_add_by_doi(config, entries, source=""):
        sources.append(source)
        return {"created": {"10.1/a": {"key": "K", "citation_key": "c"}},
                "skipped_existing": [], "failed": {}}

    monkeypatch.setattr(module, "add_by_doi", fake_add_by_doi)
    monkeypatch.setattr(sys, "argv", ["ingest_add.py", "--entries", str(entries), *extra])
    module.main()
    return sources


def test_creates_and_finishes_its_own_run(tmp_path, monkeypatch):
    sources = _run(tmp_path, monkeypatch)
    (run_dir,) = (tmp_path / "ingestion" / "runs").iterdir()
    assert sources == [f"ingestion/{run_dir.name}"]
    assert json.loads((run_dir / "added.json").read_text())["created"]["10.1/a"]["key"] == "K"
    assert json.loads((run_dir / "work" / "entries.json").read_text())["entries"][0]["doi"] == "10.1/a"
    assert read_manifest(run_dir)["status"] == "ok"


def test_leaves_the_skills_open_run_for_the_skill_to_close(tmp_path, monkeypatch):
    run_dir = tmp_path / "ingestion" / "runs" / "2026-10-09-1500"
    (run_dir / "work").mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"status": "running"}))
    sources = _run(tmp_path, monkeypatch, "--run", str(run_dir))
    assert sources == ["ingestion/2026-10-09-1500"]
    assert (run_dir / "added.json").is_file()
    assert read_manifest(run_dir)["status"] == "running"
