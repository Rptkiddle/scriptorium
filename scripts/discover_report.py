"""discovery, report stage: the PRISMA-style flow report and include set.

Usage:
    uv run python scripts/discover_report.py --run <rundir>

Renders report.md (flow counts from prisma.json, the verified include
list with provenance and screening reasons, relative-recall outcome,
stopping rule) and includes.json (the vetted set, ready for the ingest
handoff), then finishes the run: "ok", or "partial" when includes still
await verification, with the question and include count recorded in
run.json and <data>/discovery/INDEX.md.
"""

import argparse
from pathlib import Path

from scriptorium.discovery import candidates_path, load_json, save_json
from scriptorium.runs import finish


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run_dir = Path(args.run).expanduser()
    protocol = load_json(run_dir / "protocol.json", None)
    prisma = load_json(run_dir / "prisma.json", None)
    candidates = load_json(candidates_path(run_dir), [])

    by_status: dict = {}
    for c in candidates:
        by_status.setdefault(c["status"], []).append(c)
    # Only verified includes go to the handoff; unverified ones (verify not
    # run, or includes added after it) are reported, never handed over.
    unverified = [c for c in by_status.get("include", []) if not c.get("verified")]
    includes = [c for c in by_status.get("include", []) if c.get("verified")]
    if unverified:
        print(f"WARNING: {len(unverified)} include(s) not verified — run"
              " discover_verify.py before handing over; they are left out of includes.json")
    q = protocol["question"]

    lines = [f"# discovery — {run_dir.name}", "", f"**Question:** {q}", ""]
    lines += ["## Flow", ""]
    total_hits = sum(x["hits"] for x in prisma["queries"])
    d = prisma.get("dedup", {})
    lines += [
        f"- identified: {total_hits} records"
        f" ({len(prisma['queries'])} queries across Semantic Scholar and OpenAlex)",
        f"- after deduplication: {d.get('unique', '?')} unique"
        f" · already in library/known: {d.get('already_in_library', '?')} removed",
    ]
    for r in prisma["rounds"]:
        if r.get("stage") == "snowball":
            lines.append(
                f"- snowball round {r['round']}: {r['seeds']} seeds ->"
                f" {r['backward']} references + {r['forward_kept']} citing"
                f" (of {r['forward_seen']}) -> {r['new_candidates']} new candidates")
        elif r.get("stage") == "cap":
            lines.append(f"- round {r['round']}: {r['dropped']} candidates"
                         " beyond the screening cap (ranked out, not judged)")
        elif r.get("stage") == "screen":
            lines.append(
                f"- screen round {r['round']}: {r['screened']} judged ->"
                f" {r['include']} include, {r['exclude']} exclude"
                f" ({r['arbitrated']} settled by the arbiter)")
    v = prisma.get("verify", {})
    lines += [
        f"- verification: {v.get('checked', 0)} checked,"
        f" {v.get('failed', 0)} failed",
        f"- **included: {len(includes)}**",
        "",
    ]
    rc = prisma.get("relative_recall", {})
    if rc.get("known"):
        lines += [f"Relative recall: the query set re-found {rc['refound']} of"
                  f" {rc['known']} known-relevant papers"
                  + (f" (missed: {', '.join(rc['missed'])})" if rc.get("missed") else ""),
                  ""]
    if prisma.get("stop"):
        lines += [f"Stopping rule: {prisma['stop']}", ""]

    lines += ["## Included", ""]
    for c in sorted(includes, key=lambda c: (c["round"], c["id"])):
        authors = ", ".join(c["authors"][:3]) + (" et al." if len(c["authors"]) > 3 else "")
        lines += [
            f"- **{c['title']}** — {authors} ({c.get('year', 'n.d.')})."
            f" https://doi.org/{c['doi']}",
            f"  found by {'/'.join(c['provenance'][:3])} (round {c['round']})"
            f" — {c.get('why', '')}",
        ]
    vf = by_status.get("verify_failed", [])
    if vf:
        lines += ["", "## Failed verification (NOT added)", ""]
        lines += [f"- {c['title'][:80]} — {c.get('why', '')}" for c in vf]

    (run_dir / "report.md").write_text("\n".join(lines) + "\n")
    save_json(run_dir / "includes.json", [
        {"doi": c["doi"], "title": c["title"],
         "authors": ", ".join(c["authors"]), "abstract": c["abstract"],
         "venue": c["venue"], "year": c.get("year"),
         "why": c.get("why", ""), "provenance": c["provenance"],
         "round": c["round"]}
        for c in includes
    ])

    finish(run_dir, "partial" if unverified else "ok",
           f"{q} ({len(includes)} included)")
    print(f"report: {run_dir / 'report.md'}")
    print(f"includes: {len(includes)} verified papers ready for ingest handoff")



if __name__ == "__main__":
    main()
