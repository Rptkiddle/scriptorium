"""attach: attach the PDFs of a finished fetch run to their library items.

Reads <run>/fetch.jsonl and attaches every PDF that passed the fetch
check (outcome ok, file in pdfs/) through hub_attachment_link, which
copies the file into the linked-attachments folder, names it by citation
key and full-text indexes the paper (or leaves it to the next sync when one
is running). Each item is re-checked live first: one that has gained a PDF
since the fetch is skipped, so nothing is attached twice. Work goes in
batches with a pause between them; outcomes are appended to attached.jsonl,
and a re-run skips items already attached. The staged PDFs are kept; they
are deleted separately once the result has been checked.

Usage:
    uv run python scripts/attach_pdfs.py --run <run folder>
        [--batch 100] [--pause 20] [--limit N]

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="the fetch run folder")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--pause", type=float, default=20, help="seconds between batches")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    run_dir = Path(args.run).expanduser()
    fetched = read_jsonl(run_dir / "fetch.jsonl")
    done_path = run_dir / "attached.jsonl"
    done = read_jsonl(done_path)
    queue = [e for e in fetched.values()
             if e["outcome"] == "ok" and e["key"] not in done
             and (run_dir / "pdfs" / f"{e['key']}.pdf").is_file()]
    if args.limit:
        queue = queue[: args.limit]
    print(f"{len(queue)} PDFs to attach ({len(done)} attached earlier)")

    reader = ZoteroReader(load_config())
    write_run = f"retrieval/{run_dir.name}"
    try:
        with done_path.open("a") as out:
            for i, e in enumerate(queue, 1):
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
                            "pdf_path": str(run_dir / "pdfs" / f"{e['key']}.pdf"),
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
