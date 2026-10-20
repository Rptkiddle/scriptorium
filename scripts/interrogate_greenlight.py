"""interrogation skill, Step 2: green-light pass (prompt assembly).

Lists the papers admitted by the confirmed filters (the retrieval pool) and
renders the green-light prompt. A subagent judges, from short-form citations
alone, which papers should be forced into retrieval regardless of cosine
rank — recall insurance against dense retrieval's misses (scriptorium's
analogue of PaperQA2's metadata-based paper selection).

Usage:
    uv run python scripts/interrogate_greenlight.py --run <rundir> \
        [--creator S]... [--year Y]... [--year-from A --year-to B] \
        [--venue "V"] "<query 1>" "<query 2>" ...

Writes: <rundir>/work/pool.json, <rundir>/work/greenlight_prompt.txt
The subagent writes: <rundir>/work/greenlight_response.json (JSON array of
citation_key strings; may be empty).
"""

import argparse
import json
from pathlib import Path

from scriptorium.hub_client import add_filter_args, call_tool, filters_from
from scriptorium.runs import work_dir

GREENLIGHT_TEMPLATE = """You are an expert researcher reviewing a library's holdings against the
search queries below. From the paper list, hand-select the papers that will
certainly or almost certainly be directly relevant to the queries: papers
you recognise and know bear on this topic, and papers whose citation (title,
authors, venue, year) lets you confidently reason that they do — including
recent or lesser-known work you may not recognise. Judge ONLY from the
citations given. The bar is certain or near-certain direct relevance, not
plausible adjacency; selected papers are guaranteed a full-text relevance
assessment, so a wrong inclusion wastes that assessment. An empty list is a
valid answer.

Queries:
{queries}

Papers ({n}):
{papers}

Respond ONLY with a JSON array of citation_key strings, for example:
["key1", "key2"]"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    add_filter_args(ap)
    ap.add_argument("queries", nargs="+")
    args = ap.parse_args()

    filters = filters_from(args)

    work = work_dir(Path(args.run).expanduser())

    pool = call_tool("hub_search_pool", {"filters": filters} if filters else {})

    (work / "pool.json").write_text(json.dumps(pool, indent=2, ensure_ascii=False))
    prompt = GREENLIGHT_TEMPLATE.format(
        queries="\n".join(f"  {i}. {q}" for i, q in enumerate(args.queries, 1)),
        n=len(pool),
        papers="\n".join(f"- {p['citation_key']}: {p['citation']}" for p in pool),
    )
    (work / "greenlight_prompt.txt").write_text(prompt)
    print(f"pool: {len(pool)} papers (filters: {json.dumps(filters) if filters else 'none'})")
    print("greenlight_prompt.txt written")


if __name__ == "__main__":
    main()
