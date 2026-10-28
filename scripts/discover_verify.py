"""discovery, verification stage: bibliographic check of every include.

Usage:
    uv run python scripts/discover_verify.py --run <rundir>

Applies the citation-verification rule to each include: Crossref AND
Semantic Scholar must agree with the candidate's title, year, and first
author. Verified includes keep status "include" with verified=true;
failures become status "verify_failed" with the reason (they appear in
the report but are never added). When Crossref or Semantic Scholar
cannot be reached, the include stays unverified and a re-run retries it.
Slow by design (1 req/s to Semantic Scholar).
"""

import argparse
from pathlib import Path

from scriptorium.apis import http_session
from scriptorium.discovery import (
    LOOKUP_FAILED,
    candidates_path,
    load_json,
    save_json,
    verify_include,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run_dir = Path(args.run).expanduser()
    candidates = load_json(candidates_path(run_dir), [])
    prisma = load_json(run_dir / "prisma.json", None)
    session = http_session("discovery")

    includes = [c for c in candidates
                if c["status"] == "include" and not c.get("verified")]
    newly_verified = retry = 0
    for n, c in enumerate(includes, 1):
        if n % 10 == 0:  # checkpoint: a crash loses at most ten lookups
            save_json(candidates_path(run_dir), candidates)
        reason = verify_include(session, c)
        if reason is None:
            c["verified"] = True
            newly_verified += 1
        elif reason.startswith(LOOKUP_FAILED):
            c["verify_error"] = reason
            retry += 1
            print(f"  RETRY LATER {c['id']} {c['title'][:60]}: {reason}")
        else:
            c["status"] = "verify_failed"
            c["why"] = f"{c.get('why', '')} | verification: {reason}"
            print(f"  FAILED {c['id']} {c['title'][:60]}: {reason}")
    verified = [c for c in candidates if c.get("verified")]
    failed = [c for c in candidates if c["status"] == "verify_failed"]
    pending = [c for c in candidates if c["status"] == "include" and not c.get("verified")]
    prisma["verify"] = {"checked": len(verified) + len(failed), "failed": len(failed),
                        "pending": len(pending)}
    save_json(candidates_path(run_dir), candidates)
    save_json(run_dir / "prisma.json", prisma)
    print(f"verified {newly_verified}/{len(includes)} includes"
          + (f"; {retry} could not be looked up — re-run this script" if retry else ""))


if __name__ == "__main__":
    main()
