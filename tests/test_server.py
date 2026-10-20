"""The daemon starts only when run: importing its modules has no side
effects. And the surveillance page's data, including its path guard."""

import json
import threading

import pytest

from scriptorium.config import Config


def test_importing_the_daemon_modules_starts_nothing():
    from scriptorium import daemon, routes, server, tools  # noqa: F401

    with pytest.raises(RuntimeError):
        daemon.current()
    names = {t.name for t in threading.enumerate()}
    assert not names & {"idle-reaper", "auto-sync"}


def _run(runs, name, accepted=(), created=None):
    d = runs / name
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"status": "ok", "harvest": {"candidates": 1}}))
    (d / "accepted.json").write_text(json.dumps(list(accepted)))
    if created is not None:
        (d / "added.json").write_text(json.dumps({"created": created}))


def test_runs_payload_lists_runs_newest_first_and_opens_the_latest(tmp_path):
    from scriptorium.surveillance_page import runs_payload

    runs = tmp_path / "surveillance" / "runs"
    _run(runs, "2026-10-09-0200")
    _run(runs, "2026-10-10-0200", accepted=[{"doi": "10.1/a"}],
         created={"10.1/a": {"key": "K"}})
    (runs / "2026-10-10-0300").mkdir()  # stopped before harvesting
    p = runs_payload(Config(index_dir=tmp_path), None)
    assert [r["name"] for r in p["runs"]] == ["2026-10-10-0200", "2026-10-09-0200"]
    assert p["run"] == "2026-10-10-0200"
    assert p["accepted"] == [{"doi": "10.1/a"}] and p["added"] == {"10.1/a": {"key": "K"}}


def test_runs_payload_opens_only_an_existing_run_by_exact_name(tmp_path):
    from scriptorium.surveillance_page import runs_payload

    runs = tmp_path / "surveillance" / "runs"
    _run(runs, "2026-10-10-0200")
    cfg = Config(index_dir=tmp_path)
    assert runs_payload(cfg, "2026-10-09-0200")["run"] is None
    assert runs_payload(cfg, "../surveillance")["run"] is None
    assert runs_payload(cfg, "2026-10-10-0200")["run"] == "2026-10-10-0200"


def test_health_requests_are_left_out_of_the_access_log():
    import logging

    from scriptorium.server import QuietHealth

    def record(path):
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                                 '%s - "%s %s HTTP/1.1" %d', ("127.0.0.1:1", "GET", path, 200), None)

    f = QuietHealth()
    assert not f.filter(record("/health"))
    assert f.filter(record("/mcp"))
