"""attach: attach the PDFs of a finished fetch run to their library items.

Reads <run>/fetch.jsonl and attaches every PDF that passed the fetch
check (outcome ok, file in pdfs/) through hub_attachment_link, which
copies the file into the linked-attachments folder, names it by citation
key and full-text indexes the paper (or leaves it to the next sync when one
is running). PDFs held for review (pdfs-review/, their title check failed)
are attached only when accepted, or when today's title check passes them. Each item is re-checked live first: one that has gained a PDF
since the fetch is skipped, so nothing is attached twice. Work goes in
batches with a pause between them; outcomes are appended to attached.jsonl,
and a re-run skips items already attached. The staged PDFs are kept; they
are deleted separately once the result has been checked.

Review of the held PDFs (no Zotero writes):
    --review               lists them by venue: the Zotero title beside the
                           start of the PDF's first page, and any decision
    --accept KEY ... / --reject KEY ... [--note TEXT]
                           records a decision in <run>/review.jsonl; a
                           rejected paper counts for the fetch as tried and
                           missed. Files are never deleted here.

Usage:
    uv run python scripts/attach_pdfs.py --run <run folder>
        [--batch 100] [--pause 20] [--limit N]
    uv run python scripts/attach_pdfs.py --run <run folder> --review
    uv run python scripts/attach_pdfs.py --run <run folder> --accept|--reject KEY ... [--note TEXT]

Requires the daemon and Zotero (with the bridge plugin) to be running.
"""

import argparse
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from scriptorium.config import load_config
from scriptorium.hub_client import call_tool
from scriptorium.pdf_routes import (
    REVIEW_LOG,
    first_lines,
    review_decisions,
    title_on_first_pages,
)
from scriptorium.runs import finish
from scriptorium.zotero import ZoteroReader


def read_jsonl(path: Path) -> dict:
    last = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            last[entry["key"]] = entry
    return last


def index_note(indexed: dict) -> str:
    if indexed.get("ingested"):
        return "indexed"
    if indexed.get("deferred"):
        return "index_deferred"
    if indexed.get("error"):
        return "index_error"
    return "index_other"


def held_pdfs(run_dir: Path, fetched: dict) -> dict[str, dict]:
    """The run's PDFs held for review: key -> its fetch entry."""
    return {k: e for k, e in fetched.items() if e["outcome"] == "title_mismatch"
            and (run_dir / "pdfs-review" / f"{k}.pdf").is_file()}


def review_status(run_dir: Path, entry: dict, decisions: dict) -> str:
    if decision := decisions.get(entry["key"]):
        return "accepted" if decision["decision"] == "accept" else "rejected"
    pdf = (run_dir / "pdfs-review" / f"{entry['key']}.pdf").read_bytes()
    if title_on_first_pages(pdf, entry.get("title", "")):
        return "passes today's title check (attached with the run)"
    return "undecided"


def list_review(run_dir: Path, held: dict) -> None:
    decisions = review_decisions(run_dir)
    by_venue: dict[str, list[dict]] = {}
    for e in held.values():
        by_venue.setdefault(e.get("venue") or e.get("publisher", ""), []).append(e)
    counts: Counter = Counter()
    for venue, entries in sorted(by_venue.items(), key=lambda kv: -len(kv[1])):
        print(f"\n## {venue} ({len(entries)})")
        for e in entries:
            status = review_status(run_dir, e, decisions)
            counts[status.split(" (")[0]] += 1
            pdf = (run_dir / "pdfs-review" / f"{e['key']}.pdf").read_bytes()
            print(f"- {e['key']} [{status}]\n    Zotero: {e.get('title', '')[:150]}"
                  f"\n    PDF:    {first_lines(pdf)}")
    print("\n" + ", ".join(f"{k} {v}" for k, v in counts.most_common()) or "nothing held")


def record_decisions(run_dir: Path, held: dict, keys: list[str], decision: str,
                     note: str, by: str) -> None:
    unknown = [k for k in keys if k not in held]
    if unknown:
        raise SystemExit(f"not held for review in this run: {', '.join(unknown)}")
    at = datetime.now(UTC).isoformat(timespec="seconds")
    with (run_dir / REVIEW_LOG).open("a") as out:
        for k in keys:
            out.write(json.dumps({"key": k, "decision": decision, "at": at, "by": by,
                                  "note": note}, ensure_ascii=False) + "\n")
    print(f"{decision}: {len(keys)} recorded in {run_dir / REVIEW_LOG}")


def attach_queue(run_dir: Path, fetched: dict, done: dict) -> list[tuple[dict, Path]]:
    """(fetch entry, file) for every PDF to attach: those that passed the
    fetch's title check, and held ones accepted in review or passed by
    today's title check (never rejected ones)."""
    queue = [(e, run_dir / "pdfs" / f"{e['key']}.pdf") for e in fetched.values()
             if e["outcome"] == "ok" and (run_dir / "pdfs" / f"{e['key']}.pdf").is_file()]
    decisions = review_decisions(run_dir)
    for key, e in held_pdfs(run_dir, fetched).items():
        if review_status(run_dir, e, decisions) not in ("undecided", "rejected"):
            queue.append((e, run_dir / "pdfs-review" / f"{key}.pdf"))
    return [(e, path) for e, path in queue if e["key"] not in done]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="the fetch run folder")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--pause", type=float, default=20, help="seconds between batches")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--review", action="store_true", help="list the PDFs held for review")
    ap.add_argument("--accept", nargs="+", metavar="KEY", help="accept held PDFs")
    ap.add_argument("--reject", nargs="+", metavar="KEY", help="reject held PDFs")
    ap.add_argument("--note", default="", help="why, recorded with --accept/--reject")
    ap.add_argument("--by", default="user", help="who decided, recorded with --accept/--reject")
    args = ap.parse_args()

    run_dir = Path(args.run).expanduser()
    fetched = read_jsonl(run_dir / "fetch.jsonl")
    if args.review or args.accept or args.reject:
        held = held_pdfs(run_dir, fetched)
        if args.accept:
            record_decisions(run_dir, held, args.accept, "accept", args.note, args.by)
        if args.reject:
            record_decisions(run_dir, held, args.reject, "reject", args.note, args.by)
        if args.review:
            list_review(run_dir, held)
        return
    done_path = run_dir / "attached.jsonl"
    done = read_jsonl(done_path)
    queue = attach_queue(run_dir, fetched, done)
    if args.limit:
        queue = queue[: args.limit]
    print(f"{len(queue)} PDFs to attach ({len(done)} attached earlier)")

    reader = ZoteroReader(load_config())
    write_run = f"retrieval/{run_dir.name}"
    try:
        with done_path.open("a") as out:
            for i, (e, pdf_path) in enumerate(queue, 1):
                rec = reader.get_paper(e["key"])
                entry = {"key": e["key"], "doi": e["doi"]}
                if rec is None:
                    entry["outcome"] = "item_not_found"
                elif rec.pdf_status == "ok":
                    entry["outcome"] = "already_has_pdf"
                else:
                    started = time.monotonic()
                    try:
                        result = call_tool("hub_attachment_link", {
                            "parent_key": e["key"],
                            "pdf_path": str(pdf_path),
                            "run": write_run})
                    except Exception as exc:  # noqa: BLE001 — record and go on
                        # The call can fail after the attachment was made
                        # (e.g. while indexing): look before calling it failed.
                        after = reader.get_paper(e["key"])
                        if after is not None and after.pdf_status == "ok":
                            entry.update(outcome="attached", index="index_unknown")
                        else:
                            entry["outcome"] = f"attach_failed: {type(exc).__name__}"
                    else:
                        entry["outcome"] = "attached"
                        entry["index"] = index_note(result.get("indexed") or {})
                    entry["seconds"] = round(time.monotonic() - started, 1)
                entry["at"] = datetime.now(UTC).isoformat(timespec="seconds")
                out.write(json.dumps(entry) + "\n")
                out.flush()
                done[e["key"]] = entry
                print(f"{i:5}/{len(queue)} {entry['outcome']} {entry.get('index', '')} {e['doi']}",
                      flush=True)
                if i % args.batch == 0 and i < len(queue):
                    time.sleep(args.pause)
    finally:
        totals = Counter(d["outcome"] for d in done.values())
        indexed = Counter(d.get("index") for d in done.values() if d.get("index"))
        timed = [d["seconds"] for d in done.values() if d.get("seconds") is not None]
        summary = ("attach: " + ", ".join(f"{k} {v}" for k, v in sorted(totals.items()))
                   + " · " + ", ".join(f"{k} {v}" for k, v in sorted(indexed.items()))
                   + (f" · {sum(timed) / len(timed):.1f} s per attach" if timed else ""))
        with (run_dir / "report.md").open("a") as rep:
            rep.write(f"\n## Attach\n\n{summary}\n")
        failed = sum(v for k, v in totals.items() if k.startswith("attach_failed"))
        finish(run_dir, "partial" if failed else "ok", summary)
        print(summary)


if __name__ == "__main__":
    main()
