"""interrogation skill, Step 7: the discovery brief.

Every interrogation ends with a discovery brief — an assessment of the
question against the current library holdings, written for the discovery
spoke (which searches OUTSIDE the library). Assembles the brief prompt from
the run's own artifacts; interrogate_package.py saves the finished brief
as the run's brief.md.

Usage:
    uv run python scripts/interrogate_brief.py --question "..." --run <rundir>

Reads:  <rundir>/work/pool.json, greenlight_response.json, filtering.md,
        answer_final.md (else answer_response.txt, else none)
Writes: <rundir>/work/brief_prompt.txt
"""

import argparse
import json
from pathlib import Path

from scriptorium.runs import work_dir

BRIEF_TEMPLATE = """You are assessing a personal research library's coverage of a question and
writing a discovery brief: the handoff document for a subsequent
literature-discovery step that searches OUTSIDE this library.

Question: {question}

The interrogation's artifacts follow.

Library papers considered (the filtered pool): {pool_n}
Papers hand-selected as relevant by expert review of citations:
{greenlight}

Relevance filtering outcome (per retrieved chunk; levels: 0 irrelevant,
1 contextual, 2 supporting, 3 central):
{filtering}

Answer produced:
{answer}

Write the discovery brief with exactly these six sections:

1. Verdict — one sentence: the library answered this fully / partially /
   not at all, with the key numbers (chunks judged, survivors).
2. What the library establishes — the answer's main claims, each with its
   source papers. Grounded in the answer above only.
3. Relevant holdings (exclusion list) — every library paper that carried
   evidence (with its highest relevance level) plus the green-lit papers.
   Discovery must not re-acquire any of these. Note any green-lit paper
   whose chunks all scored 0: the library appears to cover the topic there,
   but the text did not deliver.
4. Gaps — what the question needs that the holdings did not supply:
   constructs, populations, methods, time periods. Grounded in the contrast
   between the question and sections 2-3.
5. Search directives — where and how discovery should look: search terms,
   authors, venues, year ranges, citation-chase anchors, each with a
   one-line rationale. This section draws on your general knowledge and is
   therefore ungrounded: mark clearly speculative leads as such, and prefer
   precise, checkable pointers over broad advice.
6. Protocol seed — a fenced ```json code block, nothing else in the
   section, with EXACTLY these keys (the discovery pipeline parses it, so
   it must be valid JSON with these key names):
   {{"question": the question verbatim,
     "concept_blocks": 2-4 arrays of search terms — one array per concept
       (phenomenon, context, optional method), each enumerating synonyms,
       spelling variants, and near-synonyms (4-8 terms per block),
     "include_criteria": eligibility tests a paper must ALL satisfy,
       written neutrally, applicable by a stranger,
     "exclude_criteria": conditions any ONE of which excludes a paper,
     "year_from": integer or null, "year_to": integer or null,
     "seed_citekeys": citation keys of the strongest library papers to
       snowball from (from section 3),
     "covered_citekeys": citation keys of ALL library papers in section 3
       (discovery must not re-acquire them)}}

Be concise and specific. The brief will be used directly to drive
discovery, so do not add any extraneous information."""


def _read(path: Path, fallback: str) -> str:
    return path.read_text().strip() if path.exists() else fallback


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True)
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    work = work_dir(Path(args.run).expanduser())

    pool_path = work / "pool.json"
    pool_n = str(len(json.loads(pool_path.read_text()))) if pool_path.exists() else "(unknown)"
    gl_path = work / "greenlight_response.json"
    greenlight = (
        "\n".join(f"- {k}" for k in json.loads(gl_path.read_text())) if gl_path.exists() else "(none)"
    ) or "(none)"
    filtering = _read(work / "filtering.md", "(relevance scoring not run)")
    answer = _read(work / "answer_final.md", _read(work / "answer_response.txt", "I cannot answer."))

    prompt = BRIEF_TEMPLATE.format(
        question=args.question, pool_n=pool_n, greenlight=greenlight,
        filtering=filtering, answer=answer,
    )
    (work / "brief_prompt.txt").write_text(prompt)
    print(f"brief_prompt.txt written (pool: {pool_n}, answer: {len(answer)} chars)")



if __name__ == "__main__":
    main()
