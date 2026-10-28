"""surveillance, add stage: put a run's accepted papers into the library.

Usage:
    uv run python scripts/surveil_add.py --run <rundir> [--dry-run]
    uv run python scripts/surveil_add.py --retry-pending --into <rundir>

Reads <rundir>/accepted.json, places each paper in its venue's registry
collection, tags it (surveillance, plus expansion/reference by justification
prefix), and stores the justification in Extra. Skips anything already in
the library. Writes <rundir>/added.json and work/add_attempt_N.json, and
regenerates the run's digest when it already has one, so the run's
summary records the additions (--dry-run previews without writing).
--retry-pending re-runs the add for every other marked run within the
catch-up window and records the outcome in <into>/retried.json. The logic
lives in scriptorium.surveillance_cycle.
"""

import argparse
import json
import sys
from pathlib import Path

from scriptorium.config import load_config
from scriptorium.surveillance_cycle import (
    _entries,
    add_run,
    add_summary,
    retry_pending,
    write_digest,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run")
    mode.add_argument("--retry-pending", action="store_true")
    ap.add_argument("--into", help="current run dir (with --retry-pending)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    config = load_config()
    if args.retry_pending:
        if not args.into:
            sys.exit("--retry-pending needs --into <current rundir>")
        retry_pending(config, Path(args.into).expanduser())
        return

    run_dir = Path(args.run).expanduser()
    accepts = json.loads((run_dir / "accepted.json").read_text())
    if not accepts:
        print("no accepts — nothing to add")
        return
    if args.dry_run:
        for e in _entries(config, accepts):
            print(f"would add {e['doi']} -> collection {e['collection_key'] or '(none)'}"
                  f" tags={','.join(e['tags'])}")
        return
    print(add_summary(add_run(config, run_dir, by="session")))
    if (run_dir / "digest.md").exists():
        write_digest(run_dir, by="session")


if __name__ == "__main__":
    main()
