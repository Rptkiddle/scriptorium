"""Held PDFs: review decisions, what attach takes, and what fetch learns."""

import importlib.util
import json
from pathlib import Path

import pymupdf
import pytest


def _script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_pdf(text):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


@pytest.fixture
def run(tmp_path):
    """A fetch run: one PDF that passed, four held for review (one of which
    today's title check passes)."""
    d = tmp_path / "2026-10-15-172608-fetch"
    (d / "pdfs").mkdir(parents=True)
    (d / "pdfs-review").mkdir()
    entries = [("OK1", "ok", "A Study of Things"), ("ACC", "title_mismatch", "Wanted Paper"),
               ("REJ", "title_mismatch", "Another Paper"), ("UND", "title_mismatch", "Third Paper"),
               ("NOW", "title_mismatch", "A Paper Now Matched")]
    for key, outcome, title in entries:
        folder = "pdfs" if outcome == "ok" else "pdfs-review"
        text = title if key in ("OK1", "NOW") else "Some other text entirely"
        (d / folder / f"{key}.pdf").write_bytes(make_pdf(text))
    (d / "fetch.jsonl").write_text("".join(
        json.dumps({"key": k, "outcome": o, "title": t, "venue": "V", "doi": f"10.1/{k}"}) + "\n"
        for k, o, t in entries))
    return d


def test_decisions_shape_the_attach_queue(run):
    attach = _script("attach_pdfs")
    fetched = attach.read_jsonl(run / "fetch.jsonl")
    held = attach.held_pdfs(run, fetched)
    assert sorted(held) == ["ACC", "NOW", "REJ", "UND"]
    attach.record_decisions(run, held, ["ACC"], "accept", "checked by eye", "user")
    attach.record_decisions(run, held, ["REJ"], "reject", "a commentary bundle", "user")
    queue = attach.attach_queue(run, fetched, done={})
    assert [(e["key"], p.parent.name) for e, p in queue] == [
        ("OK1", "pdfs"), ("ACC", "pdfs-review"), ("NOW", "pdfs-review")]
    assert [e["key"] for e, _ in attach.attach_queue(run, fetched, done={"OK1": {}})] == ["ACC", "NOW"]
    decisions = attach.review_decisions(run)
    assert decisions["REJ"]["note"] == "a commentary bundle"
    assert attach.review_status(run, held["UND"], decisions) == "undecided"


def test_a_decision_on_a_paper_not_held_is_refused(run):
    attach = _script("attach_pdfs")
    held = attach.held_pdfs(run, attach.read_jsonl(run / "fetch.jsonl"))
    with pytest.raises(SystemExit, match="not held for review"):
        attach.record_decisions(run, held, ["OK1"], "accept", "", "user")
    assert not (run / "review.jsonl").exists()


def test_a_rejected_pdf_counts_for_the_fetch_as_tried_and_missed(run):
    attach, fetch = _script("attach_pdfs"), _script("fetch_pdfs")
    held = attach.held_pdfs(run, attach.read_jsonl(run / "fetch.jsonl"))
    attach.record_decisions(run, held, ["REJ"], "reject", "", "user")
    found = fetch.earlier_outcomes(run.parent)
    assert found["REJ"] == ("rejected in review", run.name)
    assert found["UND"] == ("staged", run.name)
