"""discovery, snowball stage: citation chasing into round N.

Usage:
    uv run python scripts/discover_snowball.py --run <rundir> --round N

Round 1 chases from the protocol's seed DOIs plus round 0's includes;
later rounds chase only from the previous round's includes (Wohlin
iteration). Backward chasing takes all references; forward chasing keeps
the top `forward_cap` citing papers per seed, ranked by concept-term
overlap. New unique candidates (not in the library, not already seen)
join candidates.json as round N pending; abstracts are backfilled.
"""

import argparse
from pathlib import Path

from scriptorium.apis import http_session
from scriptorium.config import load_config
from scriptorium.discovery import (
    already_held,
    backfill_abstracts,
    candidates_path,
    dedup_key,
    fuzzy_key,
    library_titles,
    load_json,
    merge_candidates,
    protocol_terms,
    save_json,
    snowball,
)
from scriptorium.zotero import ZoteroReader, dois_of


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--round", type=int, required=True)
    args = ap.parse_args()
    run_dir = Path(args.run).expanduser()
    protocol = load_json(run_dir / "protocol.json", None)
    prisma = load_json(run_dir / "prisma.json", None)
    candidates = load_json(candidates_path(run_dir), [])

    include_dois = [c["doi"] for c in candidates
                    if c["status"] == "include" and c["doi"]
                    and c["round"] == args.round - 1]
    seeds = [f"DOI:{d}" for d in include_dois]
    if args.round == 1:
        seeds += [f"DOI:{d}" for d in protocol["seed_dois"]
                  if d not in include_dois]
    if not seeds:
        print("no seeds for this round — nothing to chase (convergence)")
        return

    session = http_session("discovery")
    new_cands, stats = snowball(session, seeds, protocol["caps"]["forward_cap"],
                                protocol_terms(protocol), f"r{args.round}")
    pool = {dedup_key(c): c for c in candidates}
    pool_fuzzy = {fk for c in candidates if (fk := fuzzy_key(c))}
    library = ZoteroReader(load_config()).library_snapshot()
    lib_dois, lib_titles = dois_of(library), library_titles(library)
    known = set(protocol["covered_dois"]) | set(protocol["seed_dois"])
    fresh: dict = {}
    merge_candidates(fresh, new_cands)
    added = []
    next_n = len(candidates) + 1
    for key, c in fresh.items():
        if (key in pool or (fuzzy_key(c) or "") in pool_fuzzy
                or already_held(c, lib_dois, lib_titles)
                or (c["doi"] and c["doi"] in known)):
            continue
        c["id"] = f"d{next_n:05d}"
        c["round"] = args.round
        c["status"] = "pending"
        next_n += 1
        added.append(c)
    still_missing = backfill_abstracts(session, {c["id"]: c for c in added})
    candidates += added
    prisma["rounds"] = [r for r in prisma["rounds"]
                        if not (r.get("round") == args.round and r.get("stage") == "snowball")]
    prisma["rounds"].append({"round": args.round, "stage": "snowball", **stats,
                             "new_candidates": len(added),
                             "no_abstract": still_missing})
    save_json(candidates_path(run_dir), candidates)
    save_json(run_dir / "prisma.json", prisma)
    print(f"round {args.round}: {stats['seeds']} seeds ->"
          f" {stats['backward']} refs + {stats['forward_kept']}/{stats['forward_seen']}"
          f" citing kept -> {len(added)} new candidates"
          f" ({still_missing} without abstract)")
    if stats["seed_errors"]:
        print(f"seed errors: {stats['seed_errors']}")



if __name__ == "__main__":
    main()
