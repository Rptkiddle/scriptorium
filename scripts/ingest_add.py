"""ingest: add verified papers to the library, by DOI or by their own metadata.

Usage:
    uv run python scripts/ingest_add.py --entries <entries.json> [--run <rundir>] [--dry-run]

entries.json is a list of entries, each {"collection_key", "tags": [str],
"extra": str} plus either "doi" (with any fallback metadata: title,
authors, abstract, venue, year) or "item" (complete Zotero item JSON, for a
paper without a Crossref DOI). The caller (the ingest skill, or discovery's
handoff) resolves collection placement and verification before invoking;
this script is the deterministic tail (scriptorium.additions.add_papers):
dedupe against the library, apply the published-version rule, fetch full
Crossref records, create through the bridge. Every add belongs to an ingestion run:
--run names the ingest skill's open run; without it a run is created here
and finished with this add's summary. The entries are copied to
<rundir>/work/entries.json and the result written to <rundir>/added.json;
every write is attributed to the run in the write log. Prints one line per
entry (by DOI, else arXiv ID, URL or title) and a summary.

--dry-run writes nothing to Zotero: it prints what would happen (including
preprint/published version matches). With --run it also saves that as
<rundir>/work/dry_run.json; without --run it is a pure preview: no run
folder is created and nothing is saved.
"""

import argparse
import json
from pathlib import Path

from scriptorium.additions import add_papers
from scriptorium.config import load_config
from scriptorium.runs import finish, new_run, work_dir


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entries", required=True)
    ap.add_argument("--run", help="the ingestion run folder (default: create one)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen; write nothing to Zotero")
    args = ap.parse_args()

    config = load_config()
    # A dry run without --run is a pure preview: no run folder, nothing saved.
    own_run = not args.run and not args.dry_run
    run_dir = (Path(args.run).expanduser() if args.run
               else new_run(config, "ingestion") if own_run else None)
    source_file = Path(args.entries).expanduser()
    entries = json.loads(source_file.read_text())
    if run_dir is not None:
        (work_dir(run_dir) / "entries.json").write_text(json.dumps(
            {"from": str(source_file), "entries": entries}, indent=1, ensure_ascii=False))

    result = add_papers(config, entries,
                        source=f"ingestion/{run_dir.name}" if run_dir else "ingestion/preview",
                        dry_run=args.dry_run)
    if args.dry_run:
        if run_dir is not None:
            (work_dir(run_dir) / "dry_run.json").write_text(json.dumps(result, indent=1))
        for doi in result["would_create"]:
            print(f"would create {doi}")
    else:
        (run_dir / "added.json").write_text(json.dumps(result, indent=1))
    for doi, c in result["created"].items():
        print(f"created {doi} -> {c['citation_key']} ({c['key']})")
    for doi in result["skipped_existing"]:
        print(f"already in library: {doi}")
    for doi, v in result.get("versions", {}).items():
        if v["case"] == "recorded":
            done = "would not be created, would be recorded" if args.dry_run else \
                "not created, recorded"
            print(f"preprint of {v['matched_key']} (another version, {v['how']}):"
                  f" {done} in its Extra: {doi}")
        else:
            print(f"replaces preprint {v['matched_key']} ({v['how']}; preprint"
                  f" {v.get('preprint', '')}): {doi}")
    for doi, reason in result["failed"].items():
        print(f"FAILED {doi}: {reason[:120]}")
    for doi, reason in result.get("unverified", {}).items():
        print(f"created from fallback metadata (Crossref lookup failed — check it): {doi}: {reason[:100]}")
    summary = (f"created {len(result['created'])},"
               f" already-present {len(result['skipped_existing'])},"
               f" other-version {len(result.get('versions', {}))},"
               f" failed {len(result['failed'])},"
               f" unverified {len(result.get('unverified', {}))}")
    print(f"summary: {summary}")
    print(f"run folder: {run_dir}" if run_dir else "preview only: nothing saved, no run folder")
    if own_run and not args.dry_run:
        (run_dir / "report.md").write_text(f"# ingestion — {run_dir.name}\n\n{summary}\n")
        finish(run_dir, "partial" if result["failed"] else "ok", summary)


if __name__ == "__main__":
    main()
