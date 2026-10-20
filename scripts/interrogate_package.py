"""interrogation skill, packaging step: the run's deliverables.

An interrogation is expensive (many subagents, tens of minutes), so its
results are written for reuse at the root of the run directory
(<data>/interrogation/runs/<YYYY-MM-DD-HHMMSS>-<keyphrase>/), beside the
work/ folder that holds every intermediate file:

    report.md        the cited answer (numeric citations + references)
    references.bib   BibLaTeX for every source paper, abstracts included,
                     fielded from Zotero (metadata already verified at ingest)
    sources.md       every judged chunk: the judge's evidence summary and
                     anchored relevance level, survivors in full
    brief.md         the discovery brief (input contract for the discovery
                     spoke)

It then finishes the run (scriptorium.runs): run.json records the question,
counts and source keys, and <data>/interrogation/INDEX.md, the catalogue a
later session checks before re-interrogating, gains the run's line.

Usage:
    uv run python scripts/interrogate_package.py --question "..." --run <rundir>
"""

import argparse
import json
import re
import sys
from pathlib import Path

from scriptorium.config import load_config
from scriptorium.interrogation import bib_entry
from scriptorium.runs import finish, work_dir
from scriptorium.zotero import ZoteroReader, citation_key_of


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True)
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run_dir = Path(args.run).expanduser()
    work = work_dir(run_dir)

    def read(name: str) -> str | None:
        path = work / name
        return path.read_text() if path.is_file() else None

    chunks = json.loads(read("chunks.json") or "[]")
    filtering = read("filtering.md") or ""
    cutoff_m = re.search(r"cutoff (\d+)", filtering)
    cutoff = int(cutoff_m.group(1)) if cutoff_m else 2

    judged_file = read("judged.json")
    if judged_file is None:
        sys.exit("work/judged.json is missing: run interrogate_assemble.py first")
    # interrogate_assemble's record: the exact scores and sources the answer used.
    judged = json.loads(judged_file)
    survivors = [j for j in judged if j["kept"]]
    source_keys = sorted({j["citation_key"] for j in survivors})

    answer = read("answer_final.md") or read("answer_response.txt") or (
        "The library could not answer this question: no retrieved chunk"
        " survived relevance scoring at the configured cutoff."
    )
    q = args.question.replace("\n", " ").strip()
    (run_dir / "report.md").write_text(f"# {q}\n\n{answer.strip()}\n")
    brief = read("brief_response.md")
    if brief:
        (run_dir / "brief.md").write_text(brief.strip() + "\n")

    lines = [f"# Sources — {q}", "",
             f"{len(judged)} chunks judged · {len(survivors)} survived"
             f" (cutoff {cutoff}) · {len(source_keys)} source papers", ""]
    for ck in source_keys:
        mine = [j for j in survivors if j["citation_key"] == ck]
        lines += [f"## {ck}", f"{mine[0]['citation']}", ""]
        for j in sorted(mine, key=lambda j: -j["score"]):
            sim = f", similarity {j['similarity']:.3f}" if j["similarity"] is not None else ""
            pages = f", pages {j['pages']}" if j["pages"] else ""
            lines += [f"- **{j['key']}** (level {j['score']}{sim}{pages}):"
                      f" {j['summary']}", ""]
    discarded = [j for j in judged if j not in survivors]
    if discarded:
        lines += ["## Judged but discarded", ""]
        lines += [f"- {j['key']} (level {j['score']})" for j in discarded]
    (run_dir / "sources.md").write_text("\n".join(lines) + "\n")

    wanted = set(source_keys)
    by_ck = {}
    for data in ZoteroReader(load_config()).library_snapshot():
        if (ck := citation_key_of(data)) in wanted:
            by_ck[ck] = data
    entries, missing = [], []
    for ck in source_keys:
        if ck in by_ck:
            entries.append(bib_entry(ck, by_ck[ck]))
        else:
            missing.append(ck)
    (run_dir / "references.bib").write_text("\n\n".join(entries) + "\n")

    finish(run_dir, "ok", f"{q} ({len(source_keys)} sources)",
           question=q,
           counts={"chunks_judged": len(judged), "survivors": len(survivors),
                   "cutoff": cutoff},
           source_keys=source_keys)

    print(f"run: {run_dir}")
    for name in ("report.md", "references.bib", "sources.md", "brief.md"):
        mark = "ok" if (run_dir / name).is_file() else "MISSING"
        print(f"  {name}: {mark}")
    if missing:
        print(f"  WARNING: no Zotero item found for citation keys: {', '.join(missing)}")



if __name__ == "__main__":
    main()
