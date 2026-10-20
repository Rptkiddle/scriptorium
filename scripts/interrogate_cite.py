"""interrogation skill, Step 6: numeric citation rendering.

Deterministic post-processing of the answer subagent's output: context keys
in in-text citations become [n] numbers (ordered by first appearance), and a
References block maps every number back to its context key, full citation,
and page range — the key is retained so the session can back-match sources
after the interrogation. The answer prompt itself is untouched (the subagent
keeps writing PaperQA2-style keys; this is presentation only).

Usage:
    uv run python scripts/interrogate_cite.py --run <rundir>

Reads:  <rundir>/work/answer_response.txt, <rundir>/work/chunks.json
Writes: <rundir>/work/answer_final.md (and prints it)
"""

import argparse
import json
from pathlib import Path

from scriptorium.interrogation import number_citations
from scriptorium.runs import work_dir


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    work = work_dir(Path(args.run).expanduser())

    answer = (work / "answer_response.txt").read_text().strip()
    # Only sources that survived relevance scoring are citable; a key for a
    # retrieved-but-discarded chunk stays un-numbered and is reported.
    by_key = {s["key"]: s for s in json.loads((work / "survivors.json").read_text())}

    final, stray = number_citations(answer, by_key)
    (work / "answer_final.md").write_text(final)
    print(final)
    if stray:
        print(f"WARNING: {len(stray)} cited key(s) are not surviving sources and were"
              f" left un-numbered: {', '.join(stray)}")



if __name__ == "__main__":
    main()
