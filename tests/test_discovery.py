"""Discovery: dedup/merge, queries, verdict collection, verification."""

import json

import pytest

from scriptorium import discovery
from scriptorium.runs import work_dir
from scriptorium.discovery import (
    _oa_abstract, build_queries, collect_verdicts, dedup_key, lex_score,
    merge_candidates, verify_include,
)

PROTOCOL = {
    "question": "How is X studied?",
    "concept_blocks": [["serendipity", "unexpectedness"],
                       ["recommender system", "collaborative filtering"]],
}


def _cand(**over):
    base = {"doi": "10.1/a", "title": "A Paper", "abstract": "", "year": 2020,
            "venue": "V", "authors": ["Ada Lovelace"], "s2_id": "", "openalex_id": "",
            "sources": ["s2"], "provenance": ["q1"]}
    return {**base, **over}


def test_oa_abstract_reconstruction():
    inv = {"world": [1], "hello": [0], "again": [2]}
    assert _oa_abstract(inv) == "hello world again"
    assert _oa_abstract(None) == ""


def test_dedup_key_doi_vs_fuzzy():
    assert dedup_key(_cand()) == "doi:10.1/a"
    fuzzy = dedup_key(_cand(doi="", title="A Paper!", year=2020))
    assert fuzzy == dedup_key(_cand(doi="", title="a paper", year=2020))
    assert fuzzy != dedup_key(_cand(doi="", title="a paper", year=2021))


def test_merge_accumulates_and_fills():
    pool: dict = {}
    assert merge_candidates(pool, [_cand()]) == 1
    added = merge_candidates(pool, [_cand(provenance=["q2"], sources=["openalex"],
                                          abstract="found it", s2_id="s2x")])
    assert added == 0
    held = pool["doi:10.1/a"]
    assert held["provenance"] == ["q1", "q2"]
    assert held["sources"] == ["openalex", "s2"]
    assert held["abstract"] == "found it"
    assert held["s2_id"] == "s2x"


def test_build_queries_two_blocks_no_relaxations():
    queries = build_queries(PROTOCOL)
    notes = [q["note"] for q in queries]
    assert "strict conjunction, all blocks" in notes
    assert not any("relaxation" in n for n in notes)
    strict = queries[0]["expr"]
    assert '"recommender system"' in strict and "serendipity" in strict


def test_build_queries_three_blocks_pairwise():
    protocol = {**PROTOCOL,
                "concept_blocks": PROTOCOL["concept_blocks"] + [["news"]]}
    relaxations = [q for q in build_queries(protocol) if "relaxation" in q["note"]]
    assert len(relaxations) == 3


def test_lex_score():
    terms = {"serendipity", "recommender system"}
    assert lex_score(_cand(title="Serendipity in a recommender system"), terms) == 2
    assert lex_score(_cand(title="Unrelated"), terms) == 0


def _write_batch(run_dir, ids, verdicts):
    work = work_dir(run_dir)
    (work / "screen_r0_batch_1_expected.json").write_text(json.dumps(ids))
    (work / "screen_r0_batch_1_response.json").write_text(json.dumps(verdicts))


def test_collect_verdicts_happy(tmp_path):
    _write_batch(tmp_path, ["d1", "d2"],
                 [{"id": "d1", "verdict": "include", "why": "w"},
                  {"id": "d2", "verdict": "unsure", "why": ""}])
    verdicts = collect_verdicts(tmp_path, 0)
    assert verdicts["d1"]["verdict"] == "include"
    assert verdicts["d2"]["verdict"] == "unsure"


def test_collect_verdicts_id_drift_raises(tmp_path):
    _write_batch(tmp_path, ["d1", "d2"],
                 [{"id": "d1", "verdict": "include", "why": ""},
                  {"id": "d99", "verdict": "exclude", "why": ""}])
    with pytest.raises(RuntimeError, match="batch_1"):
        collect_verdicts(tmp_path, 0)


def test_collect_verdicts_missing_response_raises(tmp_path):
    (work_dir(tmp_path) / "screen_r0_batch_1_expected.json").write_text('["d1"]')
    with pytest.raises(RuntimeError, match="missing"):
        collect_verdicts(tmp_path, 0)


def test_collect_verdicts_bad_verdict_raises(tmp_path):
    _write_batch(tmp_path, ["d1"], [{"id": "d1", "verdict": "maybe", "why": ""}])
    with pytest.raises(RuntimeError, match="invalid"):
        collect_verdicts(tmp_path, 0)


def _mock_request(crossref, s2):
    def fake(session, method, url, params=None, json_body=None):
        if "crossref" in url:
            return {"message": crossref}
        return s2
    return fake


CR_OK = {"title": ["A Paper"], "author": [{"family": "Lovelace",
                                           "sequence": "first"}],
         "issued": {"date-parts": [[2020]]}}
S2_OK = {"title": "A Paper", "year": 2020, "authors": [{"name": "Ada Lovelace"}]}


def test_verify_agreement_passes(monkeypatch):
    monkeypatch.setattr(discovery, "_request", _mock_request(CR_OK, S2_OK))
    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    assert verify_include(None, _cand()) is None


def test_verify_compound_surname_passes(monkeypatch):
    cr = {**CR_OK, "author": [{"family": "Paulino de Lima", "sequence": "first"}]}
    s2 = {**S2_OK, "authors": [{"name": "F. Paulino de Lima"}]}
    monkeypatch.setattr(discovery, "_request", _mock_request(cr, s2))
    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    assert verify_include(None, _cand(authors=["F. Paulino de Lima"])) is None


def test_verify_subtitle_split_passes(monkeypatch):
    cr = {**CR_OK, "title": ["A Paper"]}
    monkeypatch.setattr(discovery, "_request", _mock_request(cr, S2_OK))
    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    assert verify_include(
        None, _cand(title="A Paper: With a Longer Subtitle Crossref Omits Entirely Here")
    ) is None


def test_verify_wrong_title_fails(monkeypatch):
    cr = {**CR_OK, "title": ["Entirely Different Work"]}
    monkeypatch.setattr(discovery, "_request", _mock_request(cr, S2_OK))
    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    assert "title mismatch" in verify_include(None, _cand())


def test_verify_wrong_year_fails(monkeypatch):
    cr = {**CR_OK, "issued": {"date-parts": [[2015]]}}
    monkeypatch.setattr(discovery, "_request", _mock_request(cr, S2_OK))
    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    assert "year mismatch" in verify_include(None, _cand(year=2020))


def test_verify_no_doi_fails():
    assert "no DOI" in verify_include(None, _cand(doi=""))


# -- review fixes: dedup cross-match, arbiter verdicts, robustness ----------

import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from scriptorium.discovery import (  # noqa: E402
    _first_surname, already_held, fuzzy_key,
)


def _rc(doi, title="Serendipity in Recommender Systems", year=2020,
          authors=("Ana da Silva",)):
    return {"doi": doi, "title": title, "year": year, "authors": list(authors),
            "abstract": "", "venue": "", "s2_id": "", "openalex_id": "",
            "sources": ["s2"], "provenance": ["q"]}


def test_merge_matches_doi_and_doiless_copies_of_one_paper():
    pool: dict = {}
    assert merge_candidates(pool, [_rc("")]) == 1
    assert merge_candidates(pool, [_rc("10.1/x")]) == 0
    assert list(pool) == ["doi:10.1/x"]
    assert pool["doi:10.1/x"]["doi"] == "10.1/x"


def test_empty_titles_never_fuzzy_match():
    assert fuzzy_key(_rc("", title="日本語のタイトル")) is None
    pool: dict = {}
    merge_candidates(pool, [_rc("", title="日本語"), _rc("", title="中文")])
    assert len(pool) == 2


def test_first_surname_handles_empty_authors():
    assert _first_surname([]) == "" and _first_surname([""]) == ""
    assert _first_surname(["Ana da Silva"]) == "silva"


def test_already_held_by_title_when_no_doi():
    assert already_held(_rc(""), set(), {"serendipityinrecommendersystems"})
    assert not already_held(_rc("", title=""), set(), {""})


def _wb(run_dir, stage, ids, verdicts, wrap=False):
    work = work_dir(run_dir)
    (work / f"{stage}_r0_batch_1_expected.json").write_text(json.dumps(ids))
    body = json.dumps([{"id": i, "verdict": v} for i, v in zip(ids, verdicts)])
    (work / f"{stage}_r0_batch_1_response.json").write_text(
        f"```json\n{body}\n```" if wrap else body)


def test_arbiter_may_not_answer_unsure(tmp_path):
    _wb(tmp_path, "arbiter", ["d00001"], ["unsure"])
    with pytest.raises(RuntimeError, match="bad verdict"):
        collect_verdicts(tmp_path, 0, "arbiter")


def test_collect_accepts_fenced_json(tmp_path):
    _wb(tmp_path, "screen", ["d00001"], ["include"], wrap=True)
    assert collect_verdicts(tmp_path, 0, "screen")["d00001"]["verdict"] == "include"


def test_verify_fails_on_uncomparable_title(monkeypatch):
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)
    monkeypatch.setattr(discovery, "_request", lambda *a, **k: {
        "message": {"title": ["X"]}, "title": "X"})
    reason = verify_include(None, {**_rc("10.1/x", title="日本語"), "authors": []})
    assert reason and "comparable" in reason


# -- interrogation: scores 0-3 only; only survivors are citable --------------

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _interrogation_run(tmp_path, responses):
    chunks = [{"citation_key": "smith2020", "chunk_index": i, "name": f"smith2020 pages {i}",
               "citation": "Smith (2020) T.", "pages": str(i)} for i in range(len(responses))]
    work = work_dir(tmp_path)
    (work / "chunks.json").write_text(json.dumps(chunks))
    (work / "rcs").mkdir()
    for i, r in enumerate(responses):
        (work / "rcs" / f"chunk_{i:02d}_response.json").write_text(r)


def _run(script, *args):
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                          capture_output=True, text=True)


def test_assemble_rejects_scores_outside_0_to_3(tmp_path):
    _interrogation_run(tmp_path, ['{"summary": "s", "relevance_score": "1/3"}'])
    out = _run("interrogate_assemble.py", "--question", "q", "--run", str(tmp_path), "--cutoff", "2")
    assert out.returncode == 1 and "lack a 0-3 relevance_score" in out.stdout


def test_cite_numbers_only_surviving_sources(tmp_path):
    _interrogation_run(tmp_path, ['{"summary": "kept", "relevance_score": 3}',
                                  '{"summary": "weak", "relevance_score": 1}'])
    assert _run("interrogate_assemble.py", "--question", "q", "--run", str(tmp_path),
                "--cutoff", "2").returncode == 0
    assert [s["key"] for s in json.loads((tmp_path / "work" / "survivors.json").read_text())] == [
        "smith2020-c0"]
    (tmp_path / "work" / "answer_response.txt").write_text("A claim (smith2020-c0). Another (smith2020-c1).")
    out = _run("interrogate_cite.py", "--run", str(tmp_path))
    assert "[1]" in out.stdout and "smith2020-c1" in out.stdout.split("WARNING")[1]


def test_verify_leaves_unreachable_lookups_for_a_rerun(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    from scriptorium import discovery
    path = Path(__file__).resolve().parents[1] / "scripts" / "discover_verify.py"
    spec = importlib.util.spec_from_file_location("discover_verify_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    run_dir = tmp_path / "run"
    (run_dir / "work").mkdir(parents=True)
    cands = [{"id": i, "status": "include", "title": i, "doi": f"10.1/{i}"}
             for i in ("ok", "down", "wrong")]
    discovery.save_json(discovery.candidates_path(run_dir), cands)
    discovery.save_json(run_dir / "prisma.json", {})
    reasons = {"ok": None, "down": f"{discovery.LOOKUP_FAILED}: timeout",
               "wrong": "title mismatch vs crossref"}
    monkeypatch.setattr(module, "verify_include", lambda s, c: reasons[c["id"]])
    monkeypatch.setattr(module, "http_session", lambda agent: None)
    monkeypatch.setattr(sys, "argv", ["discover_verify.py", "--run", str(run_dir)])
    module.main()

    status = {c["id"]: (c["status"], c.get("verified", False))
              for c in discovery.load_json(discovery.candidates_path(run_dir), [])}
    assert status == {"ok": ("include", True), "down": ("include", False),
                      "wrong": ("verify_failed", False)}
    prisma = discovery.load_json(run_dir / "prisma.json", {})
    assert prisma["verify"] == {"checked": 2, "failed": 1, "pending": 1}
