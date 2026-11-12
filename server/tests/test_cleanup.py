"""Cleaning stored passages of download stamps: a dry run changes nothing;
applying shortens or deletes passages, records every change, and makes the
new vectors; a second run finds nothing."""

import json

from scriptorium_server import jobs
from scriptorium_server.cleanup import clean_stamps
from scriptorium_server.embedding import FakeEmbedder

PROSE = "the model of the network is a way to see how the group and its members act in the world"
STAMP = ("Downloaded from https://www.pnas.org by EXAMPLE UNIVERSITY on August 24, 2026"
         " from IP address 192.0.2.10.")
VEC = "[" + ",".join(["0.0625"] * 256) + "]"


def fill(db):
    db.execute("INSERT INTO items (key, type, title, title_norm, provenance_kind)"
               " VALUES ('K1', 'journalArticle', 'T', 't', 'donation')")
    for idx, part, text in [(0, "body", f"{PROSE} {STAMP}"), (1, "footnote", STAMP),
                            (2, "body", PROSE)]:
        db.execute("INSERT INTO passages (item, idx, part, text, embedding)"
                   " VALUES ('K1', %s, %s, %s, %s)", (idx, part, text, VEC))
    db.commit()


def passages(db):
    return [(r["idx"], r["text"], r["embedding"] is None) for r in
            db.execute("SELECT idx, text, embedding FROM passages ORDER BY idx").fetchall()]


def test_dry_run_changes_nothing_and_reports(db, tmp_path):
    fill(db)
    before = passages(db)
    counts = clean_stamps(db, tmp_path / "runs")
    assert (counts["shortened"], counts["deleted"], counts["items"]) == (1, 1, 1)
    assert passages(db) == before
    run = tmp_path / "runs" / "clean-stamps"
    (run_dir,) = run.glob("*-dry-run")
    assert "would be shortened" in (run_dir / "report.md").read_text()
    assert len((run_dir / "changes.jsonl").read_text().splitlines()) == 2


def test_apply_shortens_deletes_records_and_embeds_then_finds_nothing(db, tmp_path):
    fill(db)
    counts = clean_stamps(db, tmp_path / "runs", apply=True, embedder=FakeEmbedder())
    assert (counts["shortened"], counts["deleted"], counts["embedded"]) == (1, 1, 1)
    assert passages(db) == [(0, PROSE, False), (2, PROSE, False)]
    run_dir = sorted((tmp_path / "runs" / "clean-stamps").glob("*-apply"))[0]
    changes = [json.loads(line) for line in (run_dir / "changes.jsonl").read_text().splitlines()]
    assert [(c["idx"], c["after"]) for c in changes] == [(0, PROSE), (1, "")]
    assert changes[0]["before"] == f"{PROSE} {STAMP}"
    assert jobs.read_manifest(run_dir)["status"] == "ok"
    again = clean_stamps(db, tmp_path / "runs", apply=True, embedder=FakeEmbedder())
    assert (again["shortened"], again["deleted"]) == (0, 0)
