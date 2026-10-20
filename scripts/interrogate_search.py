# This file contains prompt text from PaperQA2 (FutureHouse,
# https://github.com/Future-House/paper-qa), Copyright 2024 FutureHouse,
# licensed under the Apache License 2.0 (LICENSES/Apache-2.0.txt).
# Changes: the summary prompt's 0-10 relevance score is replaced by an anchored 0-3 scale, and the summary length is set from the settings.
"""scriptorium skill, Step 1-2 prep: retrieval and RCS prompt rendering.

Calls hub_search_chunks on the daemon for each query, saves the full request/response
pairs, deduplicates chunks by name across calls, and renders one RCS prompt
file per chunk (PaperQA2's summary_json prompt, filled verbatim). The session
then dispatches one subagent per prompt file; chunk text never passes through
the session's own output.

Usage:
    uv run python scripts/interrogate_search.py \
        --question "..." --run <rundir> [--k 10] [query1 query2 ...]

Queries default to the question itself if none are given. Output layout:
    <rundir>/work/search_call_N.json     hub_search_chunks request + full response
    <rundir>/work/chunks.json            deduplicated chunk list (orchestration record)
    <rundir>/work/rcs/chunk_NN_prompt.txt
"""

import argparse
import asyncio
import json
from pathlib import Path

from scriptorium.config import load_config
from scriptorium.hub_client import Hub, add_filter_args, filters_from
from scriptorium.interrogation import split_judges
from scriptorium.runs import work_dir
from scriptorium.settings import load_settings

RCS_TEMPLATE = """Provide a summary of the relevant information that could help answer the
question based on the excerpt. Your summary, combined with many others, will
be given to the model to generate an answer. Respond with the following JSON
format:

{{
  "summary": "...",
  "relevance_score": 0-3
}}

where `summary` is relevant information from the text - about {summary_words}
words. `relevance_score` grades how the excerpt could function in the answer:

  0 - Irrelevant: the excerpt contains no information bearing on the
      question. Leave `summary` empty.
  1 - Contextual: background or tangentially related material - could inform
      scene-setting or definitions, but does not itself help answer the
      question.
  2 - Supporting: bears on the question with partial, indirect, or
      single-aspect evidence - would strengthen an answer, but could not
      anchor one.
  3 - Central: directly addresses the question - an answer could be built on
      this excerpt.

Excerpt from {name}: {citation}

---

{text}

---

Question: {question}

Respond ONLY with the JSON object."""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--k", type=int, default=None, help="Default: interrogation settings (else 10)")
    add_filter_args(ap)
    ap.add_argument("--force-paper", action="append", default=[],
                    help="Green-lit citation_key whose best chunks are forced into the pool (repeatable)")
    ap.add_argument("--force-chunks", type=int, default=None,
                    help="Chunks forced per green-lit paper (default: settings, else 1)")
    ap.add_argument("queries", nargs="*")
    args = ap.parse_args()

    stored = load_settings(load_config())["interrogation"]
    if args.k is None:
        args.k = int(stored.get("k", 10))
    if args.force_chunks is None:
        args.force_chunks = int(stored.get("force_chunks", 1))
    summary_words = int(stored.get("summary_words", 100))

    filters = filters_from(args)

    work = work_dir(Path(args.run).expanduser())
    (work / "rcs").mkdir(parents=True, exist_ok=True)
    queries = args.queries or [args.question]

    chunks: list[dict] = []
    seen: set[str] = set()
    total = 0
    async with Hub() as hub:
        for i, query in enumerate(queries, start=1):
            params: dict = {"query": query, "k": args.k}
            if filters:
                params["filters"] = filters
            results = await hub.call("hub_search_chunks", params)
            (work / f"search_call_{i}.json").write_text(
                json.dumps(
                    {"request": {"tool": "hub_search_chunks", **params}, "response": results},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            total += len(results)
            for res in results:
                if res["name"] not in seen:
                    seen.add(res["name"])
                    chunks.append(res)

        # Forced entries: each green-lit paper contributes its own top chunks
        # (best cosine match within that paper across all queries), merged
        # after the regular route so dedup removes overlap.
        forced: list[dict] = []
        forced_calls: list[dict] = []
        for key in args.force_paper:
            best: dict[str, dict] = {}
            for query in queries:
                params = {
                    "query": query,
                    "k": args.force_chunks,
                    "filters": {"citation_keys": [key]},
                }
                results = await hub.call("hub_search_chunks", params)
                forced_calls.append({"request": {"tool": "hub_search_chunks", **params}, "response": results})
                for res in results:
                    if res["name"] not in best or res["similarity"] > best[res["name"]]["similarity"]:
                        best[res["name"]] = res
            top = sorted(best.values(), key=lambda x: -x["similarity"])[: args.force_chunks]
            for res in top:
                if res["name"] not in seen:
                    seen.add(res["name"])
                    chunks.append(res)
                    forced.append(res)
        if forced_calls:
            (work / "forced_search.json").write_text(
                json.dumps(forced_calls, indent=2, ensure_ascii=False)
            )

    (work / "chunks.json").write_text(json.dumps(chunks, indent=2, ensure_ascii=False))
    for i, c in enumerate(chunks):
        prompt = RCS_TEMPLATE.format(
            name=c["name"], citation=c["citation"], text=c["text"],
            question=args.question, summary_words=summary_words,
        )
        (work / "rcs" / f"chunk_{i:02d}_prompt.txt").write_text(prompt)

    # Split the chunks across scoring judges: shuffled so no judge gets one
    # paper's whole run of chunks, one manifest file per judge.
    per_judge = int(stored.get("rcs_chunks_per_judge", 0))
    judges = split_judges(len(chunks), per_judge, seed=Path(args.run).name)
    n_judges = len(judges)
    for j, indices in enumerate(judges, start=1):
        names = [f"chunk_{i:02d}_prompt.txt" for i in indices]
        (work / "rcs" / f"judge_{j}_manifest.txt").write_text("\n".join(names) + "\n")

    regular = len(chunks) - len(forced)
    print(
        f"{len(queries)} search call(s), {total} results, {len(chunks)} unique chunks"
        f" ({regular} regular + {len(forced)} forced from {len(args.force_paper)} green-lit)"
    )
    print(f"  scoring judges: {n_judges} (rcs/judge_N_manifest.txt, max {per_judge or 'all'} chunks each)")
    if filters:
        print(f"  filters: {json.dumps(filters)}")
    forced_names = {c["name"] for c in forced}
    for i, c in enumerate(chunks):
        marker = " [forced]" if c["name"] in forced_names else ""
        print(f"  chunk_{i:02d}: {c['name']} (sim={c['similarity']:.3f}){marker}")


if __name__ == "__main__":
    asyncio.run(main())
