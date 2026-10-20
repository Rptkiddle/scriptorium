"""discovery, protocol stage: create a run from a confirmed search spec.

Usage:
    uv run python scripts/discover_protocol.py --spec <spec.json>

spec.json (drafted by the session from a discovery brief or with the user,
and confirmed by the user BEFORE this script runs):
    {"question", "slug", "concept_blocks": [[term, ...], ...],
     "include_criteria": [...], "exclude_criteria": [...],
     "year_from": int|null, "year_to": int|null,
     "seed_citekeys": [...], "covered_citekeys": [...]}

Resolves citation keys to DOIs against the live library (seeds drive
snowballing; covered items are the relative-recall gold set and are
excluded from candidates), attaches caps from settings, creates the run
(<data>/discovery/runs/<YYYY-MM-DD-HHMMSS>-<slug>/, scriptorium.runs), and
prints its directory.
"""

import argparse
import json
from pathlib import Path

from scriptorium.apis import norm_doi
from scriptorium.config import load_config
from scriptorium.discovery import save_json
from scriptorium.runs import new_run, update_manifest
from scriptorium.settings import load_settings
from scriptorium.zotero import ZoteroReader, citation_key_of


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    args = ap.parse_args()
    spec = json.loads(Path(args.spec).expanduser().read_text())

    for key in ("question", "slug", "concept_blocks",
                "include_criteria", "exclude_criteria"):
        if not spec.get(key):
            raise SystemExit(f"spec missing required field: {key}")
    if not 2 <= len(spec["concept_blocks"]) <= 4:
        raise SystemExit("concept_blocks must have 2-4 blocks")

    wanted = set(spec.get("seed_citekeys", []) + spec.get("covered_citekeys", []))
    doi_of: dict[str, str] = {}
    if wanted:
        for data in ZoteroReader(load_config()).library_snapshot():
            ck = citation_key_of(data)
            if ck in wanted and data.get("DOI"):
                doi_of[ck] = norm_doi(data["DOI"])
    unresolved = sorted(wanted - set(doi_of))

    config = load_config()
    caps = load_settings(config)["discovery"]
    protocol = {
        **spec,
        "seed_dois": sorted({doi_of[k] for k in spec.get("seed_citekeys", [])
                             if k in doi_of}),
        "covered_dois": sorted({doi_of[k] for k in spec.get("covered_citekeys", [])
                                if k in doi_of}),
        "caps": {k: caps[k] for k in ("per_query_cap", "forward_cap",
                                      "screen_round_cap", "snowball_rounds",
                                      "screen_batch")},
    }
    run_dir = new_run(config, "discovery", spec["slug"])
    update_manifest(run_dir, question=spec["question"])
    save_json(run_dir / "protocol.json", protocol)
    save_json(run_dir / "prisma.json", {"queries": [], "rounds": []})

    print(f"run: {run_dir}")
    print(f"blocks: {[len(b) for b in protocol['concept_blocks']]} terms;"
          f" criteria: {len(protocol['include_criteria'])} include,"
          f" {len(protocol['exclude_criteria'])} exclude")
    print(f"seeds: {len(protocol['seed_dois'])} with DOI;"
          f" covered (recall gold): {len(protocol['covered_dois'])}")
    if unresolved:
        print(f"WARNING: citekeys without a library DOI (unusable): {unresolved}")



if __name__ == "__main__":
    main()
