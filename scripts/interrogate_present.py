"""interrogation skill, closing step: the complete run package, verbatim.

Prints the full closing package from the run's own artifacts — a digest of
every step's numbers, then the final answer, then the discovery brief, both
copied exactly from disk. The orchestrating session pastes this output into
its final message unchanged; nothing is rewritten by a model.

Usage:
    uv run python scripts/interrogate_present.py --question "..." --run <rundir>

Reads:  <rundir>/work/: pool.json, greenlight_response.json, chunks.json,
        filtering.md, rcs/judge_*_manifest.txt, answer_final.md,
        brief_response.md
"""

import argparse
import json
import re
from pathlib import Path

from scriptorium.runs import work_dir

NO_ANSWER = (
    "The library could not answer this question: no retrieved chunk survived"
    " relevance scoring at the configured cutoff."
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True)
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run_dir = Path(args.run).expanduser()
    work = work_dir(run_dir)

    def read(name: str) -> str | None:
        path = work / name
        return path.read_text().strip() if path.exists() else None

    pool = json.loads(read("pool.json") or "[]")
    greenlit = json.loads(read("greenlight_response.json") or "[]")
    chunks = json.loads(read("chunks.json") or "[]")
    sims = [c["similarity"] for c in chunks if "similarity" in c]
    judges = len(list((work / "rcs").glob("judge_*_manifest.txt")))

    filtering = read("filtering.md") or ""
    scores = [int(m) for m in re.findall(r"score=(\d)", filtering)]
    tally = {level: scores.count(level) for level in (3, 2, 1, 0)}
    survived = re.search(r"(\d+) of (\d+) survived \(cutoff (\d+)\)", filtering)

    answer = read("answer_final.md")
    brief = read("brief_response.md")

    parts = [
        "**interrogation: run digest**",
        f"Question: {args.question}",
        f"Pool: {len(pool)} papers · green-lit {len(greenlit)}"
        + (": " + ", ".join(greenlit) if greenlit else ""),
        f"Chunks: {len(chunks)} unique"
        + (f" · similarity {min(sims):.3f}-{max(sims):.3f}" if sims else "")
        + f" · scoring judges: {judges}",
        (f"Scores: central {tally[3]} · supporting {tally[2]} · contextual {tally[1]}"
        f" · irrelevant {tally[0]}"),
    ]
    if survived:
        parts.append(
            f"Survivors: {survived.group(1)} of {survived.group(2)}"
            f" (cutoff {survived.group(3)}), all used as sources"
        )
    parts.append(f"Run: {run_dir} (report.md, references.bib, sources.md,"
                 " brief.md; intermediates in work/)")
    parts += ["", "**scriptorium answer**", answer or NO_ANSWER]
    if brief:
        parts += ["", "**interrogation: discovery brief**", brief]
    print("\n".join(parts))


if __name__ == "__main__":
    main()
