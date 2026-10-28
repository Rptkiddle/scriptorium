"""discovery, search stage: run the protocol's queries, build round 0.

Usage:
    uv run python scripts/discover_search.py --run <rundir>

Executes every query formulation against Semantic Scholar and OpenAlex,
merges and deduplicates hits, checks relative recall against the
protocol's known-relevant items (covered + seeds), excludes papers the
library already holds, backfills missing abstracts, and writes
candidates.json (round 0, status pending) and the prisma queries/dedup
record. Slow by design (1 req/s to Semantic Scholar).
"""

import argparse
import datetime
from pathlib import Path

from scriptorium.apis import http_session
from scriptorium.config import load_config
from scriptorium.discovery import (
    _from_openalex,
    _from_s2,
    already_held,
    backfill_abstracts,
    build_queries,
    candidates_path,
    library_titles,
    load_json,
    merge_candidates,
    save_json,
    search_openalex,
    search_s2,
)
from scriptorium.zotero import ZoteroReader, dois_of


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run_dir = Path(args.run).expanduser()
    protocol = load_json(run_dir / "protocol.json", None)
    prisma = load_json(run_dir / "prisma.json", {"queries": [], "rounds": []})
    session = http_session("discovery")
    cap = protocol["caps"]["per_query_cap"]

    pool: dict = {}
    for q in build_queries(protocol):
        try:
            if q["source"] == "s2":
                hits = search_s2(session, q["expr"], cap,
                                 protocol.get("year_from"), protocol.get("year_to"))
                cands = [_from_s2(h, q["id"]) for h in hits]
            else:
                hits = search_openalex(session, q["expr"], cap,
                                       protocol.get("year_from"), protocol.get("year_to"))
                cands = [_from_openalex(h, q["id"]) for h in hits]
        except Exception as exc:  # noqa: BLE001 — a failed query is logged, not fatal
            prisma["queries"].append({
                **q, "hits": 0, "error": str(exc),
                "ran": datetime.datetime.now().isoformat(timespec="seconds"),
            })
            print(f"{q['id']} [{q['source']}] {q['note']}: FAILED — {exc}")
            continue
        new = merge_candidates(pool, cands)
        prisma["queries"].append({
            **q, "hits": len(hits), "new_after_dedup": new,
            "ran": datetime.datetime.now().isoformat(timespec="seconds"),
        })
        print(f"{q['id']} [{q['source']}] {q['note']}: {len(hits)} hits, {new} new")

    known = set(protocol["covered_dois"]) | set(protocol["seed_dois"])
    found = {c["doi"] for c in pool.values() if c["doi"]} & known
    recall = {"known": len(known), "refound": len(found),
              "missed": sorted(known - found)}
    prisma["relative_recall"] = recall
    print(f"relative recall: {len(found)}/{len(known)} known-relevant papers"
          f" re-found by the query set")
    if known and len(found) / len(known) < 0.5:
        print("WARNING: under half re-found — consider revising the queries"
              f" (missed: {recall['missed']})")

    library = ZoteroReader(load_config()).library_snapshot()
    lib_dois, lib_titles = dois_of(library), library_titles(library)
    kept, excluded = [], 0
    for c in pool.values():
        if already_held(c, lib_dois, lib_titles) or (c["doi"] and c["doi"] in known):
            excluded += 1
            continue
        kept.append(c)
    for i, c in enumerate(kept, 1):
        c["id"] = f"d{i:05d}"
        c["round"] = 0
        c["status"] = "pending"
    still_missing = backfill_abstracts(session, {c["id"]: c for c in kept})
    prisma["dedup"] = {"unique": len(pool), "already_in_library": excluded,
                       "candidates": len(kept), "no_abstract": still_missing}
    save_json(candidates_path(run_dir), kept)
    save_json(run_dir / "prisma.json", prisma)
    print(f"candidates: {len(kept)} ({excluded} already held/known excluded;"
          f" {still_missing} still lack an abstract after backfill)")



if __name__ == "__main__":
    main()
