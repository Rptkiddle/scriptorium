"""surveillance: run one full cycle (harvest -> filter -> appraise ->
report -> add -> digest), driven by code.

Usage:
    uv run python scripts/surveil.py [--run <rundir>] [--from YYYY-MM-DD]

Creates a run folder unless --run names one. Prints the run folder, its
final status (ok, partial or failed) and the one-line summary; exits 1 when
the run failed. The nightly launcher (scriptorium.launcher) runs the same
cycle.
"""

import argparse
import sys
from pathlib import Path

from scriptorium.config import load_config
from scriptorium.runs import new_run
from scriptorium.surveillance_cycle import run_cycle


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="existing run folder (default: create one)")
    ap.add_argument("--from", dest="from_date",
                    help="harvest every venue from this date (overrides catch-up)")
    args = ap.parse_args()
    config = load_config()
    run_dir = Path(args.run).expanduser() if args.run else new_run(config, "surveillance")
    status, summary = run_cycle(config, run_dir, from_date=args.from_date)
    print(f"run folder: {run_dir}")
    print(f"status: {status}")
    print(f"summary: {summary}")
    sys.exit(1 if status == "failed" else 0)


if __name__ == "__main__":
    main()
