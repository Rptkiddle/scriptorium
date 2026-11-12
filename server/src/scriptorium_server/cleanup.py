"""Cleaning passages already in the collection when a cutting rule changes:
`scriptorium passages clean-stamps` removes publishers' download stamps
(stamps.py) from stored passages, as tei.passages() now does when it cuts
a paper.

Without `apply` nothing changes: the run folder's report says what would.
With it, each affected passage is shortened, or deleted when nothing but
the stamp was in it; a shortened passage loses its vector and is judged
again as prose or artefact, and the embedder (when given) makes the new
vectors. Every change is written to the run folder's changes.jsonl with
the passage's text before and after, so it can be undone. That file holds
the removed stamps, and with them any names and network addresses they
carried; it stays in the run folder on the server, which only admins see.
Running it again finds nothing more to do.
"""

import json
from pathlib import Path

import psycopg

from scriptorium_server import jobs
from scriptorium_server.embedding import Embedder, embed_pending_passages
from scriptorium_server.stamps import strip_stamps
from scriptorium_server.tei import PROSE_MIN, prose_share

# Passages that can hold a stamp: a cheap filter before the full rules run.
_CANDIDATES = r"ownloaded|terms of use|Terms and Conditions|licensed use|IP: "


def clean_stamps(conn: psycopg.Connection, runs_dir: Path, apply: bool = False,
                 embedder: Embedder | None = None, batch_size: int = 32) -> dict:
    """Remove download stamps from stored passages (or, without apply, report
    what would go). Returns {"looked_at", "shortened", "deleted", "items",
    "embedded", "run"}."""
    run_dir = jobs.fresh_run(runs_dir, "clean-stamps", "apply" if apply else "dry-run")
    counts = {"looked_at": 0, "shortened": 0, "deleted": 0, "items": 0, "embedded": 0,
              "run": str(run_dir)}
    items: set[str] = set()
    examples: list[tuple[str, str]] = []
    rows = conn.execute("SELECT item, idx, part, text, embed_text, artefact FROM passages"
                        " WHERE text ~ %s ORDER BY item, idx", (_CANDIDATES,)).fetchall()
    with open(run_dir / "changes.jsonl", "w") as log:
        for row in rows:
            counts["looked_at"] += 1
            text = strip_stamps(row["text"])
            if text == row["text"]:
                continue
            embed = strip_stamps(row["embed_text"]) if row["embed_text"] else None
            artefact = bool(text) and prose_share(text) < PROSE_MIN
            items.add(row["item"])
            counts["deleted" if not text else "shortened"] += 1
            if len(examples) < 20:
                examples.append((row["text"], text))
            log.write(json.dumps({"item": row["item"], "idx": row["idx"], "part": row["part"],
                                  "before": row["text"], "after": text,
                                  "embed_before": row["embed_text"], "embed_after": embed,
                                  "artefact_before": row["artefact"],
                                  "artefact_after": artefact}, ensure_ascii=False) + "\n")
            if not apply:
                continue
            if not text:
                conn.execute("DELETE FROM passages WHERE item = %s AND idx = %s",
                             (row["item"], row["idx"]))
            else:
                conn.execute("UPDATE passages SET text = %s, embed_text = %s, artefact = %s,"
                             " embedding = NULL WHERE item = %s AND idx = %s",
                             (text, embed if embed and embed != text else None, artefact,
                              row["item"], row["idx"]))
    counts["items"] = len(items)
    conn.commit()
    if apply and embedder is not None:
        counts["embedded"] = embed_pending_passages(conn, embedder, batch_size)
    (run_dir / "report.md").write_text(_report(counts, apply, examples))
    jobs.finish_run(run_dir, "ok", summary_line(counts, apply), counts=counts)
    return counts


def summary_line(counts: dict, apply: bool) -> str:
    verb = ("shortened", "deleted") if apply else ("would be shortened", "would be deleted")
    return (f"{counts['shortened']} passages {verb[0]} and {counts['deleted']} {verb[1]}"
            f" in {counts['items']} items ({counts['looked_at']} looked at)"
            + (f"; {counts['embedded']} vectors made" if counts["embedded"] else ""))


def _report(counts: dict, apply: bool, examples: list[tuple[str, str]]) -> str:
    lines = ["# Download stamps removed from passages" if apply
             else "# Download stamps in passages (dry run: nothing changed)", "",
             summary_line(counts, apply) + ".", "",
             "Every change, with the text before and after: changes.jsonl.", "",
             "## Examples", ""]
    for before, after in examples:
        lines += [f"- before: {before[:400]}", f"  after: {after[:400] or '(deleted)'}"]
    return "\n".join(lines) + "\n"
