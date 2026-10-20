"""surveillance, digest stage: the morning summary of a nightly run.

Usage:
    uv run python scripts/surveil_digest.py --run <rundir>

Writes <rundir>/digest.md (every run, quiet nights included) and finishes
the run: "partial" while additions are pending, "ok" otherwise. The logic
lives in scriptorium.surveillance_cycle.write_digest.
"""

import argparse
from pathlib import Path

from scriptorium.surveillance_cycle import write_digest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    digest, summary = write_digest(Path(args.run).expanduser())
    print(f"digest written: {digest}")
    print(f"summary: {summary}")


if __name__ == "__main__":
    main()
