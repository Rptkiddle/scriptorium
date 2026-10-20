---
name: interrogate
description: Interrogate the local scriptorium literature hub (User's Zotero library, full-text indexed) for a citation-grounded academic answer PLUS a vetted map of the supporting literature — the References list identifies which library sources bear on the question, for downstream deep reading. Invoke ONLY on explicit request — /scriptorium:interrogate (or /interrogate), "interrogate the library", "what does my library say about X", or an explicit ask for a citation-grounded/library-based answer. For ordinary academic questions, answer normally and at most OFFER this skill to the user.
---

# interrogate

Answer a question from the user's indexed Zotero library, with every claim
cited to retrieved evidence. The answer is deliberately concise; the run's
equally valuable output is its References list — library sources vetted by
relevance scoring as bearing on the question, each resolvable via
`hub_item` (metadata, PDF path) for deeper follow-up reading. A local daemon at `http://localhost:8765`
serves the index (MCP tools: `hub_search_chunks`, `hub_facets`, `hub_search_pool`,
`hub_item`; plus `GET /settings.json`). Scripts under `scripts/` do all
deterministic work; subagents perform the LLM steps; you orchestrate and
report. If the daemon is unreachable, tell the user to start it (menubar
Start, or the LaunchAgent) — never fall back to answering from your own
knowledge.

## Setup, on every invocation

0. Check `<index data dir>/interrogation/INDEX.md` for a prior run of the
   same or a closely related question. If one exists, tell the user and
   offer its results (report.md, references.bib, sources.md, brief.md in
   its run folder) before spending a fresh run; proceed only if they want a
   new pass.
1. Fetch `http://localhost:8765/settings.json`. Its `interrogation` section
   supplies the run parameters — `k`, `force_chunks`, `summary_words`,
   `rcs_cutoff`, `rcs_chunks_per_judge` — and the subagent models:
   `model_greenlight`, `model_rcs`, `model_answer`, `model_brief`. Use these
   everywhere below; the user can override any of them for this run.
2. Create the run directory (`<rundir>`):
   `uv run python -m scriptorium.runs new interrogation --slug <keyphrase>`
   (a 2-4 word kebab-case keyphrase for the question). It prints the path,
   under `<index data dir>/interrogation/runs/`, and records this session
   in the run's `run.json`. Every script below takes `--run <rundir>`;
   intermediate files go to `<rundir>/work/`, the deliverables to `<rundir>`.
3. Run all scripts from `~/Desktop/code/1_SELF/scriptorium/scriptorium` with
   `uv run python scripts/<name>.py`.

## Standing rules

- Never read chunk text or rendered prompt files into your own context, and
  never paste pipeline text into subagent prompts — subagents read prompt
  files and write response files; scripts do all parsing, filtering, and
  assembly. Your context holds only the scripts' printed summaries and the
  subagents' returned verdicts.
- The prompt templates inside the scripts are fixed text. Never edit them,
  and never reproduce their job yourself.
- Subagents are `subagent_type: "general-purpose"` with the model from
  settings — never the session default. If a subagent fails or stalls,
  check its output files on disk first: work already written stands, and
  you redispatch only what is missing. Tell the user in one line when this
  happens (e.g. "scoring stalled after chunk 18 — resumed from disk, no
  work lost").
- Answer only from evidence that survived relevance scoring. If it is
  insufficient, explain to the user why, and go to Step 7 — never fill gaps
  from your own knowledge. Your own knowledge should inform query phrasings
  and the Step 7 brief, nothing else.
- If any parameter, query, or filter changes mid-run, start over in a new
  run directory; never mix artifacts across configurations.
- When a step completes, output its templated report as message text before
  the next tool call. These mid-turn reports are best-effort (the interface
  may not display them); what is BINDING is the closing package: the run's
  final message to the user is the `interrogate_present.py` output pasted verbatim
  and in full. A run whose final
  message does not contain the full package is incomplete, even if all its
  files exist.

## Step 1 — Elucidate and confirm (user gate)

Build the search plan in this order:

1. **Detect filter features** in the question: venue/journal, author(s),
   year(s)/period. None → go to op 4.
2. **Canonicalise every detected feature externally** via Crossref or
   OpenAlex (`openalex_resolve_name` for venues/authors; web search as
   backup): establish the most likely referent and its correct full form
   ("ICA Annals" → "Annals of the International Communication Association";
   a surname → the disambiguated researcher; "this year" → concrete years).
3. **Match each canonical form against the library**:

   ```
   uv run python scripts/interrogate_facets.py --field creators|venues|years \
       --contains <substring>
   ```

   This prints the index's actual values with paper counts. Author values
   are surname keys that already merge spellings, particles and accents
   ("atteveldt" covers "van Atteveldt"), so one value per author is enough.
   Venues are raw venue strings: include every variant of the same venue
   (e.g. "New Media & Society" and "New Media \& Society"). A feature with
   no good match is FLAGGED to
   the user, never silently dropped; if resolution is ambiguous, ask the
   user.
4. **Keep filter terms out of query text.** Resolved features become
   filters only — they should not be included in the query formulations.
5. **Write 3-5 reformulations** of the question's substantive content
   (complementary phrasings, concept keywords, adjacent framings). These
   are the search queries.
6. Present the plan to the user and wait for confirmation:

```
**interrogation: search plan**
Question: <verbatim>
Interpretation: <one sentence: what will be answered>
Filters:
  - <as written> → <canonical form (source)> → library: <value (n papers) | NO MATCH — flagged>
  (or: none detected)
Queries (filter terms excluded):
  1. <phrasing 1>
  2. <phrasing 2>
  3. <phrasing 3>
Settings: k=<k> · force_chunks=<f> · summary_words=<w> · cutoff=<c: 1 contextual+ | 2 supporting+ | 3 central>
Adjust anything, or say go.
```

## Step 2 — Green-light pass (automated, no gate)

Expert selection: one subagent, acting as a domain expert, reviews the
pool's citations and hand-selects the papers it knows — or confidently
reasons from the citation — to be directly relevant to the queries. Selected
papers are guaranteed entry into relevance scoring regardless of similarity
ranking.

```
uv run python scripts/interrogate_greenlight.py --run <rundir> \
    [--creator S]... [--year Y]... [--year-from A --year-to B] [--venue "V"] \
    "<query 1>" "<query 2>" ...
```

Then dispatch ONE subagent (model: settings `model_greenlight`):

```
Read <rundir>/work/greenlight_prompt.txt and follow its instructions exactly.
Write your JSON array reply to <rundir>/work/greenlight_response.json, then
return only that same JSON array.
```

```
**interrogation: green-light**
Pool: <N> papers assessed (filters: <...|none>)
Green-lit: <M> — <key1>, <key2>, ... (or: none)
```

## Step 3 — Retrieval

```
uv run python scripts/interrogate_search.py --question "<question>" \
    --run <rundir> [filter flags as above] \
    [--force-paper KEY]... \
    "<query 1>" "<query 2>" ...
```

The script runs each query over the filtered pool (k from settings unless
`--k` is given), forces in each green-lit paper's best-matching chunk(s),
deduplicates, renders one scoring prompt file per chunk, and splits the
chunks into judge manifests (`work/rcs/judge_N_manifest.txt`, at most
`rcs_chunks_per_judge` chunks each, shuffled so no judge gets a single
paper's whole run). Its printed chunk list is all you need.

```
**interrogation: retrieval**
Queries: <n> · k=<X> · filters: <...|none>
Chunks: <total> unique (<regular> ranked + <forced> forced) ·
similarity <min>-<max> · scoring judges: <J>
```

## Step 4 — Relevance scoring

Dispatch one subagent PER manifest file (model: settings `model_rcs`), all
in a single message so they run concurrently. Each judge's prompt (fill in
its manifest number):

```
Read <rundir>/work/rcs/judge_N_manifest.txt: it lists prompt files in
<rundir>/work/rcs/. For each listed file, in order: read it, follow its
instructions exactly — base each verdict ONLY on that file's own excerpt,
never on other chunks — and write your JSON reply to the matching
chunk_XX_response.json in the same directory. When all are done, return
one line per chunk: chunk number and relevance_score.
```

When all judges finish (response files are the authoritative record), run:

```
uv run python scripts/interrogate_assemble.py --question "<question>" \
    --run <rundir> [--cutoff N]
```

The script discards chunks scoring below the cutoff (anchored 0-3 scale:
1 contextual, 2 supporting, 3 central); every survivor becomes a source in
the assembled answer prompt.

```
**interrogation: relevance scoring**
Judged: <N> chunks → <M> survived (score >= <cutoff>), all used as sources
Scores: central <n3> · supporting <n2> · contextual <n1> · irrelevant <n0>
```

## Step 5 — Answer

If the script reports no survivors: tell the user the library could not
answer and why (one sentence), skip Step 6, and continue to Step 7.
Otherwise dispatch ONE subagent (model: settings `model_answer`):

```
Read <rundir>/work/answer_prompt.txt and follow its instructions exactly.
Write your answer to <rundir>/work/answer_response.txt and also return it in
full.
```

## Step 6 — Present

```
uv run python scripts/interrogate_cite.py --run <rundir>
```

This renders `work/answer_final.md`: the answer with numeric in-text citations
([1], [2, 3]) and a References block mapping each number to its context
key, full citation, and pages — keys are kept for back-matching after the
interrogation. If any raw context key remains un-numbered in the printed
text, it was an invalid citation — report that to the user; do not edit the
file. The answer reaches the user via the Step 7 closing package.

## Step 7 — Discovery brief (always, automated)

Every interrogation ends with a discovery brief: an assessment of the
question against the current library holdings, written as the handoff for
the discovery spoke (which searches outside the library). It runs whether
the answer succeeded or not.

```
uv run python scripts/interrogate_brief.py --question "<question>" --run <rundir>
```

Dispatch ONE subagent (model: settings `model_brief`):

```
Read <rundir>/work/brief_prompt.txt and follow its instructions exactly.
Write your brief to <rundir>/work/brief_response.md and also return it in
full.
```

Then persist the run's outputs (always, even on a failed answer):

```
uv run python scripts/interrogate_package.py --question "<question>" \
    --run <rundir>
```

This writes the run's deliverables at the root of `<rundir>` — report.md
(the cited answer), references.bib (every source paper with its abstract,
fielded from Zotero), sources.md (every judged chunk with its evidence
summary and level), brief.md — and finishes the run, adding its line to
`<index data dir>/interrogation/INDEX.md`. These four files are the
interrogation's reusable surface: later sessions exploring, coding, or
writing on the topic should read them instead of re-running the pipeline.

Then produce the closing package:

```
uv run python scripts/interrogate_present.py --question "<question>" \
    --run <rundir>
```

The script prints the complete package — run digest (with the run
folder's path), answer, brief, all copied from disk.

Your final message to the user is: the script's printed package pasted
verbatim and in full (never rewritten, summarised, or regenerated), then a
note that the brief's "search directives" section draws on general
knowledge rather than the library. Close by offering the discovery spoke:

```
The brief is ready to drive a discovery run (/scriptorium:discover):
a systematic search beyond the library seeded by this interrogation.
Run it?
```

If the user instead wants another pass over the library (different
phrasings or filters), that is a fresh interrogation from Step 1 — there is
no in-skill iteration loop.

## Completion checklist

Before ending the run, check your final message. It must contain, in full:
(1) the `interrogate_present.py` package verbatim — run digest, answer with
References (or the no-survivors statement), discovery brief, run folder;
(2) the search-directives caveat; (3) the discovery offer. If anything is missing, the run is not
done — the final message is the only output the user is guaranteed to see.

## Maintenance notes

- `hub_status` shows index size, Zotero reachability, and failed items.
- After adding papers to Zotero yourself, run `hub_index` (or `hub_sync`).
- Write operations need the Zotero app running; relay its error message to
  the user rather than retrying.
