# This file contains prompt text from PaperQA2 (FutureHouse,
# https://github.com/Future-House/paper-qa), Copyright 2024 FutureHouse,
# licensed under the Apache License 2.0 (LICENSES/Apache-2.0.txt).
# Changes: example citation keys substituted; prompts assembled by this script.
"""interrogation skill, Step 4 assembly: filter scored verdicts, build the answer prompt.

Reads the per-chunk scoring responses, parses them leniently
(paperqa's llm_parse_json), applies the anchored relevance cutoff, strips
quoted in-text citations from surviving summaries (paperqa's
strip_citations, so quotes cannot collide with citation keys), sorts by
level, and writes the fully assembled answer prompt. Every survivor becomes
a source.

Usage:
    uv run python scripts/interrogate_assemble.py \
        --question "..." --run <rundir> [--cutoff N]

Reads:  <rundir>/work/chunks.json, <rundir>/work/rcs/chunk_NN_response.json
Writes: <rundir>/work/filtering.md, <rundir>/work/answer_prompt.txt
"""

import argparse
import json
from pathlib import Path

from paperqa.utils import strip_citations

from scriptorium.config import load_config
from scriptorium.interrogation import parse_rcs
from scriptorium.runs import work_dir
from scriptorium.settings import load_settings

ANSWER_TEMPLATE = """Answer in a direct and concise tone. Your audience is an expert, so be highly
specific. If there are ambiguous terms or acronyms, first define them.

Answer the question below with the context.

Context:

{context}

---

Question: {question}

Write an answer based on the context. If the context provides insufficient
information reply "I cannot answer." For each part of your answer, indicate
which sources most support it via citation keys at the end of sentences,
like ({example_key}).
Only cite from the context above and only use the citation keys from the
context.

## Valid citation examples, only use comma/space delimited parentheticals:
- ({example_key}, {second_key})
- ({example_key})
## Invalid citation examples:
- ({example_key} and {second_key})
- ({example_key};{second_key})
- Example's work ({example_key})
- Author et al. (2023)

Do not concatenate citation keys, just use them as is. Write in the style of
a scientific article, with concise sentences and coherent paragraphs. This
answer will be used directly, so do not add any extraneous information.

Answer (about 200 words, but can be longer):"""

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--cutoff", type=int, default=None,
                    help="Minimum relevance_score (0-3) to survive (default: settings, else 2)")
    args = ap.parse_args()

    if args.cutoff is None:
        args.cutoff = int(load_settings(load_config())["interrogation"]["rcs_cutoff"])

    work = work_dir(Path(args.run).expanduser())
    chunks = json.loads((work / "chunks.json").read_text())

    contexts, lines, missing, invalid, judged = [], ["# RCS filtering", ""], [], [], []
    for i, c in enumerate(chunks):
        path = work / "rcs" / f"chunk_{i:02d}_response.json"
        if not path.exists():
            missing.append(path.name)
            continue
        scored = parse_rcs(path.read_text())
        if scored is None:
            invalid.append(path.name)
            continue
        score, summary = scored
        key = f"{c['citation_key']}-c{c['chunk_index']}"
        kept = score >= args.cutoff and summary.strip()
        lines.append(f"- chunk_{i:02d} {c['name']}: score={score} {'KEPT' if kept else 'discarded'}")
        judged.append({
            "chunk": i, "key": key, "name": c["name"], "citation_key": c["citation_key"],
            "citation": c.get("citation", ""), "pages": c.get("pages", ""),
            "similarity": c.get("similarity"), "score": score, "summary": summary,
            "kept": bool(kept),
        })
        if kept:
            contexts.append(
                {"key": key, "score": score, "summary": strip_citations(summary), "citation": c["citation"]}
            )

    contexts.sort(key=lambda x: (-x["score"], x["key"]))
    top = contexts  # every survivor becomes a source; the cutoff is the selection
    lines += ["", f"{len(contexts)} of {len(chunks)} survived (cutoff {args.cutoff}); all used as sources."]
    if missing:
        lines += ["", "MISSING responses: " + ", ".join(missing)]
    if invalid:
        lines += ["", "INVALID responses (no 0-3 relevance_score): " + ", ".join(invalid)]
    (work / "filtering.md").write_text("\n".join(lines) + "\n")
    # The single record of what was judged and what became a source; cite
    # and package read these rather than re-deriving scores.
    (work / "judged.json").write_text(json.dumps(judged, indent=1, ensure_ascii=False))
    (work / "survivors.json").write_text(
        json.dumps([j for j in judged if j["kept"]], indent=1, ensure_ascii=False))

    if missing or invalid:
        if missing:
            print(f"ERROR: {len(missing)} response file(s) missing — dispatch those subagents first.")
        if invalid:
            print(f"ERROR: {len(invalid)} response file(s) lack a 0-3 relevance_score:"
                  f" {', '.join(invalid)} — delete and redispatch those chunks.")
        raise SystemExit(1)
    if not top:
        print("NO CONTEXTS SURVIVED — reply 'I cannot answer' and continue to the discovery brief (skill Step 7).")
        raise SystemExit(0)

    context_str = "\n\n".join(f"{c['key']}: {c['summary']}\nFrom {c['citation']}" for c in top)
    context_str += "\n\nValid Keys: " + ", ".join(c["key"] for c in top)
    prompt = ANSWER_TEMPLATE.format(
        context=context_str,
        question=args.question,
        example_key=top[0]["key"],
        second_key=top[1]["key"] if len(top) > 1 else top[0]["key"],
    )
    (work / "answer_prompt.txt").write_text(prompt)
    print(f"answer_prompt.txt written with {len(top)} sources:")
    for c in top:
        print(f"  {c['key']} (score {c['score']})")



if __name__ == "__main__":
    main()
