---
name: discover
description: Systematic literature discovery beyond the library - protocol-driven search of Semantic Scholar and OpenAlex, citation snowballing, criteria-based LLM screening with a different-model arbiter, bibliographic verification, and a PRISMA-style flow report. Invoke on explicit request (/scriptorium:discover (or /discover), "run discovery", "find papers on X outside the library") or from an interrogation's discovery-brief handoff.
---

# discover

A discovery run: **protocol (gate) → search → [screen → snowball]* →
verify → report → handoff (gate)**. The design and its evidence base are
in `docs/discovery.md`. Scripts (run from
the scriptorium repository (the `repo` folder `hub_status` reports) with
`uv run python scripts/<name>.py`) do all deterministic work; subagents
judge candidates by reading prompt files and writing verdict files. Never
read candidate metadata into your own context or judge relevance yourself.
Search and verification calls are slow by design (1 req/s to Semantic
Scholar) — give every script invocation a long timeout (10 minutes).

Setup: fetch `http://localhost:8765/settings.json`; the `discovery`
section supplies `model_screen` and `model_arbiter`.

## 1 — Protocol (user gate)

Draft the search spec:

- From a discovery brief (an interrogation's closing artifact): parse the
  fenced JSON in its "Protocol seed" section and take question, concept
  blocks, criteria, years, and citekeys from it.
- From a bare question: draft concept blocks (2-4: phenomenon × context ×
  optional method; 4-8 synonym variants each, covering spelling,
  hyphenation, acronyms, plurals) and neutral inclusion/exclusion
  criteria yourself, and find seed/covered papers with `hub_search_items`.

Never put the user's hypothesis or hoped-for finding into the criteria —
they are eligibility tests a stranger could apply. Write the spec JSON to
the session scratchpad and present it (blocks, criteria, years, seeds)
for confirmation. This is the run's registered protocol; wait for an
explicit go. Then:

```
uv run python scripts/discover_protocol.py --spec <spec.json>
```

Note the printed run directory (`<rundir>`, persistent under
`<data>/discovery/runs/`; intermediate files go to `<rundir>/work/`). Warn the user about any unresolved citekeys.

## 2 — Search

```
uv run python scripts/discover_search.py --run <rundir>
```

Runs every query formulation, deduplicates, excludes library holdings,
backfills abstracts, reports relative recall. If it warns that under half
the known-relevant papers were re-found, STOP and revise the concept
blocks with the user before screening a weak pool (edit the spec, start a
fresh run).

## 3 — Screen (rounds; round 0 = search results)

```
uv run python scripts/discover_screen.py --run <rundir> --round N --render
```

Dispatch one subagent PER `work/screen_rN_batch_*_prompt.txt` (all in a single
message so they run concurrently; `subagent_type: "general-purpose"`,
model: settings `model_screen`):

```
Read <rundir>/work/screen_rN_batch_M_prompt.txt and follow its instructions
exactly. Before writing, re-check the first and last id in your array
against the first and last [dNNNNN] id in the prompt; they must match
character for character. Write your JSON array reply to
<rundir>/work/screen_rN_batch_M_response.json, then return only the counts:
include X, exclude Y, unsure Z.
```

Then `--collect` (it validates every batch; redispatch only what it names
as missing or invalid). If there are unsure verdicts, `--arbitrate`,
dispatch one subagent per arbiter batch the same way (model: settings
`model_arbiter` — a DIFFERENT model from the screener, deliberately),
then `--resolve`. If there were no unsure verdicts, run `--resolve`
directly. `--resolve` prints the round summary and either
`STOP: <rule>` or `CONTINUE: snowball round N+1`.

## 4 — Snowball (while the resolver says CONTINUE)

```
uv run python scripts/discover_snowball.py --run <rundir> --round N+1
```

Then screen the new round (step 3). "no seeds" from the script means
convergence — proceed to verify.

## 5 — Verify

```
uv run python scripts/discover_verify.py --run <rundir>
```

Crossref AND Semantic Scholar must corroborate every include; failures
are reported but never added.

## 6 — Report and handoff (user gate)

```
uv run python scripts/discover_report.py --run <rundir>
```

Present the report path, the flow counts, and the included papers (title,
authors, year, venue, one-line reason). Additions are the user's call —
NEVER add without explicit confirmation. On a go: build the entries JSON
from `<rundir>/includes.json` (collection placement per the user or the
venue registry; tags `discovery` plus the run name; extra
`discovery-why: <why>`), write it to `<rundir>/work/ingest_entries.json`, then:

```
uv run python scripts/ingest_add.py --entries <rundir>/work/ingest_entries.json
```

The new items arrive without PDFs; note in the report that
`/scriptorium:retrieve` obtains them (fetch, then attach on the user's go).

Your final message: the report path, the flow summary (identified →
candidates → screened → included → verified), the stopping rule that
fired, and the handoff outcome.
