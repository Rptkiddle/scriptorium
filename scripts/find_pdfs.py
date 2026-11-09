"""ingest: find PDFs for library items with Zotero's Find Available PDF lookup.

Usage:
    uv run python scripts/find_pdfs.py --keys KEY ... [--run <rundir>]
    uv run python scripts/find_pdfs.py --keys-from <rundir>/added.json [--run <rundir>]

Each PDF found is attached the way the library keeps its PDFs (see
ZoteroWriter.find_pdf); the daemon indexes it at its next automatic sync.
Pace, concurrency, the time allowed per item and which versions and item
types are accepted come from the Zotero section of the settings page. Needs
Zotero running with Scriptorium Bridge 0.1.8 or later, and a network with
the subscriptions the PDFs require.

Prints one line per item as results arrive and writes them to
<rundir>/work/finds.jsonl, then <rundir>/found.json at the end. Without --run
a run is created here and finished with this summary. Running it again is
harmless: an item that already has a PDF is skipped.
"""

import argparse
import json
from pathlib import Path

from scriptorium.config import load_config
from scriptorium.pdf_finding import count_statuses, find_pdfs
from scriptorium.runs import finish, new_run, work_dir


def named_keys(keys: list[str], keys_from: str | None) -> list[str]:
    """Item keys named with --keys, plus the items created by the run whose
    added.json is given with --keys-from."""
    named = list(keys)
    if keys_from:
        added = json.loads(Path(keys_from).expanduser().read_text())
        named += [c["key"] for c in added.get("created", {}).values()]
    return list(dict.fromkeys(named))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", nargs="*", default=[], help="item keys")
    ap.add_argument("--keys-from", help="an added.json whose created items to use")
    ap.add_argument("--run", help="the ingestion run folder (default: create one)")
    args = ap.parse_args()

    keys = named_keys(args.keys, args.keys_from)
    if not keys:
        ap.error("give item keys with --keys or --keys-from")

    config = load_config()
    own_run = not args.run
    run_dir = Path(args.run).expanduser() if args.run else new_run(config, "ingestion")
    log_path = work_dir(run_dir) / "finds.jsonl"

    def report(key: str, result: dict) -> None:
        line = json.dumps({"key": key, **result}, ensure_ascii=False)
        with log_path.open("a") as log:
            log.write(line + "\n")
        print(line, flush=True)

    results = find_pdfs(config, keys, source=f"ingestion/{run_dir.name}", on_result=report)
    counts = count_statuses(results)
    (run_dir / "found.json").write_text(json.dumps(
        {"counts": counts, "results": results}, indent=1, ensure_ascii=False))
    summary = "found through Zotero: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    print(f"summary: {summary}")
    print(f"run folder: {run_dir}")
    if own_run:
        (run_dir / "report.md").write_text(f"# ingestion — {run_dir.name}\n\n{summary}\n")
        finish(run_dir, "partial" if counts.get("failed") else "ok", summary)


if __name__ == "__main__":
    main()
