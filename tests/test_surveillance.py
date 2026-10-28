"""Surveillance: catch-up harvest window, schedule status, filter coverage."""

import datetime
import json

import pytest

from scriptorium import surveillance, surveillance_cycle
from scriptorium.config import Config
from scriptorium.runs import new_run
from scriptorium.schedule import schedule_status
from scriptorium.surveillance import (
    FilterRetryNeeded,
    assemble_appraise,
    harvest,
    harvest_from,
    registry_path,
)

TODAY = datetime.date(2026, 9, 23)


@pytest.fixture(autouse=True)
def _no_abstract_lookups(monkeypatch):
    """The assemble step looks up missing abstracts online; tests stay offline."""
    monkeypatch.setattr(surveillance, "fill_abstracts", lambda pool, settings: {
        "missing": 0, "openalex": 0, "scopus": 0, "springer": 0,
        "unavailable": {}, "title_only": 0})


def test_harvest_from_resumes_a_day_before_watermark():
    # One day of overlap covers the local-date vs UTC boundary.
    assert harvest_from("2026-09-21", TODAY, 7) == "2026-09-20"


def test_harvest_from_caps_at_catchup_window():
    assert harvest_from("2026-09-01", TODAY, 7) == "2026-09-16"


def test_harvest_from_new_venue_starts_yesterday():
    assert harvest_from(None, TODAY, 7) == "2026-09-22"


def _at(hour):
    return datetime.datetime(2026, 9, 23, hour, 30)


def test_schedule_up_to_date_after_tonights_run():
    s = schedule_status("2026-09-23", _at(9), 2, 7)
    assert s == {"next_run": "2026-09-24T02:00:00", "pending": 0, "missed": False}


def test_schedule_before_the_hour_is_not_pending():
    s = schedule_status("2026-09-22", _at(1), 2, 7)
    assert s["pending"] == 0
    assert s["next_run"] == "2026-09-23T02:00:00"


def test_schedule_counts_pending_nights():
    s = schedule_status("2026-09-21", _at(9), 2, 7)
    assert s["pending"] == 2 and not s["missed"]


def test_schedule_missed_beyond_window():
    s = schedule_status("2026-09-14", _at(9), 2, 7)
    assert s["missed"]


def test_schedule_no_runs_yet():
    s = schedule_status(None, _at(9), 2, 7)
    assert s["pending"] == 0 and not s["missed"]


def _run(tmp_path, n_mega=4):
    run_dir = tmp_path / "run"
    (run_dir / "work").mkdir(parents=True)
    cands = [
        {"id": f"c{i:04d}", "doi": f"10.1/{i}", "title": f"T{i}", "authors": "A",
         "venue": "Mega", "mega": True, "abstract": "", "created": ""}
        for i in range(1, n_mega + 1)
    ] + [{"id": "c0099", "doi": "10.1/99", "title": "F", "authors": "B",
          "venue": "Field", "mega": False, "abstract": "x", "created": ""}]
    (run_dir / "work" / "candidates.json").write_text(json.dumps(cands))
    (run_dir / "work" / "filter_batch_1_prompt.txt").write_text("p")
    return run_dir


def _verdicts(path, pairs):
    path.write_text(json.dumps([{"id": i, "keep": k} for i, k in pairs]))


def test_assemble_full_coverage(tmp_path):
    run_dir = _run(tmp_path)
    _verdicts(run_dir / "work" / "filter_batch_1_response.json",
              [("c0001", True), ("c0002", False), ("c0003", False), ("c0004", False)])
    summary = assemble_appraise(Config(index_dir=tmp_path), run_dir)
    assert summary["filter_survivors"] == 1
    assert summary["unjudged_passed_to_appraise"] == 0
    assert summary["appraise_pool"] == 2  # c0001 + the field candidate


def test_assemble_rerenders_unjudged_once_then_passes_them_on(tmp_path):
    run_dir = _run(tmp_path)
    config = Config(index_dir=tmp_path)
    _verdicts(run_dir / "work" / "filter_batch_1_response.json",
              [("c0001", False), ("c0002", False)])
    with pytest.raises(FilterRetryNeeded) as exc:
        assemble_appraise(config, run_dir)
    assert exc.value.count == 2
    retry = run_dir / "work" / "filter_batch_2_prompt.txt"
    assert "[c0003]" in retry.read_text() and "[c0001]" not in retry.read_text()

    # The retry judge covers only one of the two; the other goes to appraise.
    _verdicts(run_dir / "work" / "filter_batch_2_response.json", [("c0003", False)])
    summary = assemble_appraise(config, run_dir)
    assert summary["unjudged_passed_to_appraise"] == 1
    assert summary["appraise_pool"] == 2  # c0004 + the field candidate


def test_assemble_fills_missing_abstracts_of_the_pool_only(tmp_path, monkeypatch):
    from scriptorium import abstracts

    sent = []

    def fake_request(session, method, url, params=None, **kw):
        sent.append(params["filter"])
        return {"results": [{"doi": "https://doi.org/10.1/1",
                             "abstract_inverted_index": {"Found": [0], "text": [1]}}]}

    monkeypatch.setattr(surveillance, "fill_abstracts", abstracts.fill_abstracts)
    monkeypatch.setattr(abstracts, "request_json", fake_request)
    run_dir = _run(tmp_path)
    _verdicts(run_dir / "work" / "filter_batch_1_response.json",
              [("c0001", True), ("c0002", False), ("c0003", False), ("c0004", False)])
    assemble_appraise(Config(index_dir=tmp_path), run_dir)
    # Only the pool's one abstract-less paper is looked up (the field paper has one).
    assert sent == ["doi:https://doi.org/10.1/1"]
    cands = {c["id"]: c for c in json.loads((run_dir / "work" / "candidates.json").read_text())}
    assert cands["c0001"]["abstract"] == "Found text"
    assert cands["c0001"]["abstract_source"] == "openalex"
    assert cands["c0002"]["abstract"] == "" and cands["c0099"]["abstract"] == "x"
    prompt = (run_dir / "work" / "appraise_batch_1_prompt.txt").read_text()
    assert "Abstract: Found text" in prompt
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["abstracts"]["openalex"] == 1 and manifest["abstracts"]["title_only"] == 0
    # Re-assembling a resumed run neither looks up again nor overwrites the counts.
    assemble_appraise(Config(index_dir=tmp_path), run_dir)
    assert len(sent) == 1
    assert json.loads((run_dir / "run.json").read_text())["abstracts"]["openalex"] == 1


# -- verdict files, appraise coverage, pending marker ------------------------

from scriptorium.surveillance import (  # noqa: E402
    AppraiseRetryNeeded,
    VerdictFileError,
    load_state,
    read_verdicts,
    write_report,
)


@pytest.mark.parametrize("text", [
    '[{"id": "c0001", "keep": true}]',
    '```json\n[{"id": "c0001", "keep": true}]\n```',
    'Here are my verdicts:\n[{"id": "c0001", "keep": true}]\nDone.',
])
def test_read_verdicts_tolerates_wrapping(tmp_path, text):
    f = tmp_path / "r.json"
    f.write_text(text)
    assert read_verdicts(f) == [{"id": "c0001", "keep": True}]


@pytest.mark.parametrize("text", ['{"id": "c1"}', "no json here", '["a", "b"]'])
def test_read_verdicts_rejects_non_arrays(tmp_path, text):
    f = tmp_path / "r.json"
    f.write_text(text)
    with pytest.raises(VerdictFileError):
        read_verdicts(f)


def _report_run(tmp_path, auto_add=0):
    config = Config(index_dir=tmp_path)
    if auto_add:
        (tmp_path / "settings.json").write_text(json.dumps({"surveillance": {"auto_add": 1}}))
    run_dir = tmp_path / "surveillance" / "runs" / "2026-09-26-0200"
    (run_dir / "work").mkdir(parents=True)
    cands = [{"id": f"c000{i}", "doi": f"10.1/{i}", "title": f"T{i}", "authors": "A",
              "venue": "V", "mega": False, "abstract": "x", "created": ""} for i in (1, 2)]
    (run_dir / "work" / "candidates.json").write_text(json.dumps(cands))
    (run_dir / "run.json").write_text(json.dumps({"harvest": {
        "date": "2026-09-26", "candidates": 2, "mega_candidates": 0,
        "field_candidates": 2,
        "venues": [{"venue": "V", "mega": False, "from": "2026-09-25",
                    "detected": 2, "new": 2}]}}))
    (run_dir / "work" / "appraise_batch_1_prompt.txt").write_text("[c0001] T1\n\n[c0002] T2\n")
    (run_dir / "work" / "appraise_batch_1_expected.json").write_text('["c0001", "c0002"]')
    return config, run_dir


def test_report_retries_unjudged_then_lists_them_and_keeps_them_unseen(tmp_path):
    config, run_dir = _report_run(tmp_path)
    (run_dir / "work" / "appraise_batch_1_response.json").write_text(
        json.dumps([{"id": "c0001", "add": True, "why": "core"}]))
    with pytest.raises(AppraiseRetryNeeded):
        write_report(config, run_dir)
    assert "[c0002]" in (run_dir / "work" / "appraise_batch_2_prompt.txt").read_text()
    assert json.loads((run_dir / "work" / "appraise_batch_2_expected.json").read_text()) == ["c0002"]
    assert load_state(config)["last_run"] is None  # state untouched

    (run_dir / "work" / "appraise_batch_2_response.json").write_text("[]")  # retry judged nothing
    report = write_report(config, run_dir)
    assert "## Not judged" in report.read_text()
    seen = load_state(config)["seen"]
    assert "10.1/1" in seen and "10.1/2" not in seen


def test_report_lives_in_the_run_folder_and_last_run_names_it(tmp_path):
    config, run_dir = _report_run(tmp_path)
    (run_dir / "work" / "appraise_batch_1_response.json").write_text(json.dumps(
        [{"id": "c0001", "add": True, "why": "core"},
         {"id": "c0002", "add": False, "why": ""}]))
    report = write_report(config, run_dir)
    assert report == run_dir / "report.md" and report.is_file()
    assert load_state(config)["last_run"]["run"] == "2026-09-26-0200"


def test_report_writes_pending_marker_only_with_auto_add(tmp_path):
    config, run_dir = _report_run(tmp_path, auto_add=1)
    (run_dir / "work" / "appraise_batch_1_response.json").write_text(json.dumps(
        [{"id": "c0001", "add": True}, {"id": "c0002", "add": False}]))
    write_report(config, run_dir)
    assert (run_dir / "add_pending.json").exists()
    assert json.loads((run_dir / "accepted.json").read_text())[0]["id"] == "c0001"


def test_report_no_pending_marker_without_auto_add(tmp_path):
    config, run_dir = _report_run(tmp_path, auto_add=0)
    (run_dir / "work" / "appraise_batch_1_response.json").write_text(json.dumps(
        [{"id": "c0001", "add": True}, {"id": "c0002", "add": False}]))
    write_report(config, run_dir)
    assert not (run_dir / "add_pending.json").exists()


def test_retry_pending_skips_current_run_and_reports_expired(tmp_path, monkeypatch):
    runs = tmp_path / "surveillance" / "runs"
    today = datetime.date.today()
    current = runs / f"{today.isoformat()}-0200"
    recent = runs / f"{(today - datetime.timedelta(days=2)).isoformat()}-0200"
    old = runs / f"{(today - datetime.timedelta(days=30)).isoformat()}-0200"
    for d in (current, recent, old):
        d.mkdir(parents=True)
        (d / "add_pending.json").write_text("{}")
        (d / "accepted.json").write_text("[]")
    (recent / "digest.md").write_text("old digest\n")
    tried = []

    def fake_add_run(config, run_dir, by="self"):
        tried.append((run_dir.name, by))
        (run_dir / "add_pending.json").unlink()
        return {"created": {}, "skipped_existing": [], "failed": {}}

    monkeypatch.setattr(surveillance_cycle, "add_run", fake_add_run)
    surveillance_cycle.retry_pending(Config(index_dir=tmp_path), current)
    assert tried == [(recent.name, current.name)]
    info = json.loads((current / "retried.json").read_text())
    assert info["expired"] == [old.name] and info["failed"] == {}
    # The retried run's digest is regenerated; the old one is kept.
    assert (recent / "work" / "digest_1.md").read_text() == "old digest\n"
    history = json.loads((recent / "run.json").read_text())["history"]
    assert history[-1]["by"] == current.name and history[-1]["status"] == "ok"


def test_add_run_records_each_attempt_and_leaves_the_report_alone(tmp_path, monkeypatch):
    run_dir = tmp_path / "surveillance" / "runs" / "2026-10-09-0200"
    (run_dir / "work").mkdir(parents=True)
    (run_dir / "accepted.json").write_text(json.dumps([{"doi": "10.1/a", "title": "T"}]))
    (run_dir / "report.md").write_text("report\n")
    (run_dir / "add_pending.json").write_text("{}")
    outcomes = [RuntimeError("Zotero closed"),
                {"created": {"10.1/a": {"key": "K", "citation_key": "c"}},
                 "skipped_existing": [], "failed": {}}]

    def fake_add_papers(config, entries, source=""):
        assert source == "surveillance/2026-10-09-0200"
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(surveillance_cycle, "add_papers", fake_add_papers)
    config = Config(index_dir=tmp_path)
    with pytest.raises(RuntimeError):
        surveillance_cycle.add_run(config, run_dir)
    surveillance_cycle.add_run(config, run_dir, by="2026-10-10-0200")
    first = json.loads((run_dir / "work" / "add_attempt_1.json").read_text())
    second = json.loads((run_dir / "work" / "add_attempt_2.json").read_text())
    assert "Zotero closed" in first["error"] and second["by"] == "2026-10-10-0200"
    assert not (run_dir / "add_pending.json").exists()
    assert (run_dir / "report.md").read_text() == "report\n"


def test_harvest_writes_candidates_from_crossref(tmp_path, monkeypatch):
    # Drives harvest() end to end with a stubbed Crossref page; the
    # 2026-10-09 crash (loop variable shadowing the work folder) failed here.
    config = Config(index_dir=tmp_path)
    reg = registry_path(config)
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps({"confirmed": True, "venues": [
        {"name": "V", "issns": ["1234-5678"], "mega": False}]}))
    page = {"items": [
        {"DOI": "10.1/A", "title": ["First"], "author": [{"family": "Ada"}]},
        {"DOI": "10.1/B", "title": ["Second"], "author": [{"family": "Bo"}]},
    ]}
    monkeypatch.setattr(surveillance, "crossref_get", lambda *a, **k: page)
    monkeypatch.setattr(surveillance.time, "sleep", lambda _: None)
    run_dir = new_run(config, "surveillance")
    summary = harvest(config, run_dir)
    assert summary["candidates"] == 2
    cands = json.loads((run_dir / "work" / "candidates.json").read_text())
    assert [c["doi"] for c in cands] == ["10.1/a", "10.1/b"]
    assert cands[0]["authors"] == "Ada"


def test_harvest_queries_every_issn_by_index_date(tmp_path, monkeypatch):
    # Querying only the first ISSN missed every paper filed under the
    # online ISSN (Physical Review E, AI Review); filtering on created date
    # missed works deposited weeks after their DOI was registered (OUP).
    config = Config(index_dir=tmp_path)
    reg = registry_path(config)
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps({"confirmed": True, "venues": [
        {"name": "V", "issns": ["1111-1111", "2222-2222", "1111-1111"], "mega": False}]}))
    calls = []

    def fake_get(session, url, **params):
        calls.append((url, params["filter"]))
        return {"items": []}

    monkeypatch.setattr(surveillance, "crossref_get", fake_get)
    monkeypatch.setattr(surveillance.time, "sleep", lambda _: None)
    harvest(config, new_run(config, "surveillance"), from_date="2026-01-01")
    url, filt = calls[0]
    assert url.endswith("/works")
    parts = filt.split(",")
    assert [p for p in parts if p.startswith("issn:")] == ["issn:1111-1111", "issn:2222-2222"]
    assert "from-index-date:2026-01-01" in parts
    # A from_date older than the ledger span lowers the created-date floor.
    assert "from-created-date:2026-01-01" in parts
    # A nightly run: the floor is the seen ledger's span.
    harvest(config, new_run(config, "surveillance", slug="nightly"))
    floor = datetime.date.today() - datetime.timedelta(days=surveillance.SEEN_LEDGER_DAYS)
    assert f"from-created-date:{floor.isoformat()}" in calls[1][1].split(",")


def test_appraise_asks_for_a_reason_on_every_verdict():
    from scriptorium.surveillance import APPRAISE_PROMPT
    assert '{"id": "c0001", "add": false, "why": "..."}' in APPRAISE_PROMPT


def test_report_lists_skips_with_reasons_and_accepts_only_adds(tmp_path):
    config, run_dir = _report_run(tmp_path)
    (run_dir / "work" / "appraise_batch_1_response.json").write_text(json.dumps(
        [{"id": "c0001", "add": True, "why": "core"},
         {"id": "c0002", "add": False, "why": "method only, off topic"}]))
    report = write_report(config, run_dir).read_text()
    accepted = json.loads((run_dir / "accepted.json").read_text())
    assert [a["id"] for a in accepted] == ["c0001"]
    assert "## Skipped" in report and "T2 — *V*. method only, off topic" in report
    appraised = json.loads((run_dir / "work" / "appraised.json").read_text())
    assert [(a["id"], a["add"], a["why"]) for a in appraised] == [
        ("c0001", True, "core"), ("c0002", False, "method only, off topic")]


def _journal(issns, current, total=0, title="Computer Law & Security Review"):
    return {"title": title, "ISSN": issns,
            "counts": {"current-dois": current, "total-dois": total}}


def test_resolve_venue_takes_the_record_with_current_papers(monkeypatch):
    # A journal that changed ISSN keeps a record for the old one; the old
    # record comes first in Crossref's results but receives no new papers.
    old, new = _journal(["0267-3649"], 0, 2889), _journal(["2212-473X"], 420, 1852)
    monkeypatch.setattr(surveillance, "crossref_get", lambda *a, **k: {"items": [old, new]})
    match, quality = surveillance._resolve_venue(None, "Computer Law & Security Review")
    assert (match["ISSN"], quality) == (["2212-473X"], "exact")


def test_resolve_venue_never_merges_journals_that_share_a_title(monkeypatch):
    sage = _journal(["1464-8849", "1741-3001"], 528, title="Journalism")
    other = _journal(["2306-4463"], 61, title="Journalism")
    monkeypatch.setattr(surveillance, "crossref_get", lambda *a, **k: {"items": [other, sage]})
    match, _ = surveillance._resolve_venue(None, "Journalism")
    assert match["ISSN"] == ["1464-8849", "1741-3001"]


def test_resolve_venue_via_openalex_takes_the_most_current_record(monkeypatch):
    records = {"0267-3649": _journal(["0267-3649"], 0, 2889),
               "2212-473X": _journal(["2212-473X"], 420, 1852)}

    def fake_get(session, url, **params):
        if url.endswith("/journals"):
            return {"items": []}  # no exact title match in Crossref's search
        return records[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(surveillance, "crossref_get", fake_get)
    monkeypatch.setattr(surveillance, "request_json", lambda *a, **k: {"results": [
        {"display_name": "Computer Law & Security Review", "issn": ["0267-3649", "2212-473X"]}]})
    match, quality = surveillance._resolve_venue(None, "Computer Law & Security Review")
    assert (match["ISSN"], quality) == (["2212-473X"], "openalex")
