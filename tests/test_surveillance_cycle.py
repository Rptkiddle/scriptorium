"""The code-driven surveillance cycle, end to end with fake Crossref and
fake judges: no network, no Claude, no Zotero."""

import json
import re

from scriptorium import judging, surveillance, surveillance_cycle
from scriptorium.config import Config
from scriptorium.runs import new_run, read_manifest
from scriptorium.surveillance import registry_path


def _ids(prompt):
    return re.findall(r"^\[(c\d+)\]", prompt.read_text(), re.MULTILINE)


def _setup(tmp_path, monkeypatch, reply):
    """A registry with one megajournal and one field venue, a stubbed
    Crossref, and a fake judge_all whose reply text comes from
    reply(stage, ids)."""
    config = Config(index_dir=tmp_path)
    reg = registry_path(config)
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps({"confirmed": True, "venues": [
        {"name": "Mega", "issns": ["1111-1111"], "mega": True},
        {"name": "Field", "issns": ["2222-2222"], "mega": False}]}))

    def fake_get(session, url, **params):
        n = "1" if "1111-1111" in params["filter"] else "2"
        return {"items": [{"DOI": f"10.{n}/{k}", "title": [f"Paper {n}{k}"],
                           "author": [{"family": "Ada"}]} for k in ("a", "b")]}

    def fake_judge(prompt, model, cwd):
        stage = "filter" if prompt.name.startswith("filter") else "appraise"
        text = reply(stage, _ids(prompt))
        name = prompt.name.removesuffix("_prompt.txt")
        if text is None:
            return {"batch": name, "model": model, "attempts": 2, "ok": False,
                    "error": "RuntimeError: exit 1"}
        prompt.with_name(f"{name}_response.json").write_text(text)
        return {"batch": name, "model": model, "attempts": 1, "ok": True,
                "model_used": ["claude-test"], "cost_usd": 0.01}

    monkeypatch.setattr(surveillance, "crossref_get", fake_get)
    monkeypatch.setattr(surveillance.time, "sleep", lambda _: None)
    monkeypatch.setattr(judging, "judge", fake_judge)
    monkeypatch.setattr(judging, "judge_all",
                        lambda prompts, model, cwd: [fake_judge(p, model, cwd) for p in prompts])
    monkeypatch.setattr(judging, "claude_version", lambda: "test")
    return config, new_run(config, "surveillance")


def _keep_all_accept_first(stage, ids):
    if stage == "filter":
        return json.dumps([{"id": i, "keep": True} for i in ids])
    return json.dumps([{"id": i, "add": i == ids[0], "why": "core"} for i in ids])


def test_cycle_runs_every_step_and_finishes_ok(tmp_path, monkeypatch):
    config, run_dir = _setup(tmp_path, monkeypatch, _keep_all_accept_first)
    status, summary = surveillance_cycle.run_cycle(config, run_dir)
    assert status == "ok", summary
    assert summary.startswith("4 candidates from 2 venues, 1 accepted")
    for name in ("report.md", "digest.md", "accepted.json"):
        assert (run_dir / name).is_file()
    manifest = read_manifest(run_dir)
    assert manifest["status"] == "ok"
    assert manifest["provenance"]["claude"] == "test"
    assert [r["batch"] for r in manifest["judging"]] == ["filter_batch_1", "appraise_batch_1"]


def test_unusable_reply_is_set_aside_and_judged_again(tmp_path, monkeypatch):
    calls = {"filter": 0}

    def reply(stage, ids):
        if stage == "filter":
            calls["filter"] += 1
            if calls["filter"] == 1:
                return "I could not decide."
        return _keep_all_accept_first(stage, ids)

    config, run_dir = _setup(tmp_path, monkeypatch, reply)
    status, _ = surveillance_cycle.run_cycle(config, run_dir)
    assert status == "ok"
    assert (run_dir / "work" / "rejected" / "filter_batch_1_response.json.1").is_file()
    assert read_manifest(run_dir)["judging"][0]["rejudged"]["ok"]


def test_failed_judge_call_leaves_candidates_unjudged_not_the_run_failed(tmp_path, monkeypatch):
    # Filter calls fail twice over: their candidates go on to Appraise
    # unjudged (the existing one-shot retry), and the run still finishes.
    def reply(stage, ids):
        return None if stage == "filter" else _keep_all_accept_first(stage, ids)

    config, run_dir = _setup(tmp_path, monkeypatch, reply)
    status, _ = surveillance_cycle.run_cycle(config, run_dir)
    assert status == "ok"
    assert read_manifest(run_dir)["assembly"]["unjudged_passed_to_appraise"] == 2


def test_code_failure_ends_the_run_as_failed_with_traceback(tmp_path, monkeypatch):
    config, run_dir = _setup(tmp_path, monkeypatch, _keep_all_accept_first)

    def broken(*a, **k):
        raise TypeError("boom")

    monkeypatch.setattr(surveillance_cycle, "assemble_appraise", broken)
    status, summary = surveillance_cycle.run_cycle(config, run_dir)
    assert status == "failed"
    assert summary == "failed at assemble: TypeError: boom"
    assert "TypeError: boom" in (run_dir / "work" / "error.txt").read_text()
    assert read_manifest(run_dir)["status"] == "failed"


def test_parse_verdicts_tolerates_wrapping_and_rejects_non_arrays():
    from scriptorium.judging import parse_verdicts

    assert parse_verdicts('Here you go:\n```json\n[{"id": "c1"}]\n```') == [{"id": "c1"}]
    assert parse_verdicts("[]") == []
    assert parse_verdicts('{"id": "c1"}') is None
    assert parse_verdicts('["c1"]') is None
    assert parse_verdicts("[not json]") is None
