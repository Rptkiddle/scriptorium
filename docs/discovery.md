# Discovery: design and procedure

The discovery spoke finds papers OUTSIDE the library that a research
question needs, following systematic-review search methodology adapted to
the three open bibliographic APIs (Semantic Scholar, OpenAlex, Crossref).
Its procedure is deliberately auditable: every query, count, and screening
decision is logged, and each run emits a PRISMA-style flow record.

## Why it works this way

Three properties of the evidence base shape the design:

1. **None of the three APIs has a controlled vocabulary** (no thesaurus,
   no proximity operators, unreliable wildcards), so free-text synonym
   expansion and citation snowballing carry the whole recall burden.
   Hybrid search — one good keyword search plus systematic snowballing —
   matches full multi-database search at lower cost (Mourão et al. 2020;
   Wohlin 2014).
2. **Recall is lost at screening, not search.** Single-screener
   title/abstract screening misses ~13% of relevant studies versus ~3%
   for dual screening (Gartlehner et al. 2020). Discovery therefore
   screens with an LLM judge against written criteria, escalates
   uncertain calls to a different, stronger model (same-model second
   passes flip ~0% of decisions), and gates every inclusion through the
   user.
3. **LLMs fabricate citations and import popularity bias.** All candidate
   metadata comes from the APIs, never from a model; candidates whose
   DOI/title/authors do not resolve are dropped, not repaired. Judges see
   title, abstract, and year only — venue, authors, and citation counts
   are withheld so prestige cannot stand in for relevance.

## Procedure

```
brief/question → protocol (user gate) → search → [screen → arbitrate →
snowball, looped while yield] → verify → report → handoff (user gate)
```

1. **Protocol.** The question is decomposed into 2–4 concept blocks
   (phenomenon × context × optional method) with enumerated synonym
   variants, plus explicit inclusion/exclusion criteria, year bounds,
   snowball seeds (known-relevant library items), and caps. The user
   confirms the protocol before any API call; it is versioned in the run
   directory.
2. **Search.** 4–8 query formulations per run: a strict all-blocks
   conjunction, two-block relaxations, and natural-language variants for
   Semantic Scholar's embedding ranking. Semantic Scholar and OpenAlex
   are both queried (source-unique records are signal: ~16% of relevant
   work is findable in only one database). The query set is validated by
   relative recall — it must re-find the protocol's known-relevant seeds,
   or it is revised before screening.
3. **Dedup and exclusion.** Normalised DOI, then fuzzy
   title+year+first-author. Items already in the library are
   excluded by live DOI check.
4. **Abstract backfill.** ~52% of Crossref and ~40% of OpenAlex records
   lack abstracts; missing abstracts are backfilled across sources before
   screening. A record still lacking one is flagged as title-only.
5. **Snowball.** Backward (references) and forward (citations) chasing
   from the seed set and each round's confirmed includes, via the
   Semantic Scholar graph. Forward citations are capped and ranked by
   lexical similarity to the protocol's concept terms before entering the
   queue.
6. **Screen.** Batches of ~25 shuffled candidates; each judged against
   the protocol criteria verbatim with a three-state verdict
   (include / exclude / unsure) and a one-line reason. Ambiguity resolves
   toward inclusion.
7. **Arbitrate.** Unsure verdicts go to a stronger model — never back to
   the same one.
8. **Stopping rule.** Snowball–screen rounds repeat until a round
   contributes under 1% of the accumulated include set (marginal yield),
   an iteration adds nothing new (Wohlin convergence), or the round cap
   is hit. The report records which rule fired.
9. **Verify.** Every include's metadata must agree between Crossref and
   Semantic Scholar (title, year within one, first author's surname),
   looked up by DOI, before it can be added; an include without a DOI
   cannot be verified.
10. **Report and handoff.** A PRISMA-style flow report (identified per
    source → deduplicated → screened → excluded with reasons → included)
    plus the include list with per-paper provenance (which query or seed
    surfaced it, which stage it survived). Additions go through the
    shared additions module on the user's confirmation, tagged
    `discovery`; PDFs follow via /scriptorium:retrieve.

## Inputs

Discovery runs from a **discovery brief** (the closing artifact of every
interrogation, which carries a machine-readable protocol seed: concept
terms, criteria, seed citation keys, gaps) or from a question stated
directly, in which case the protocol is drafted interactively at the gate.

## Outputs

A run directory, `<data>/discovery/runs/<YYYY-MM-DD-HHMMSS>-<slug>/`, in the
shared run layout: `run.json` (manifest: question, status, summary),
`protocol.json`, `prisma.json` (every query verbatim, per-source hit counts,
dedup and screening counts, per-record exclusion reasons), `report.md` (the
flow narrative and include list), `includes.json` (the vetted, verified
candidate set with provenance), and `work/` (the candidate pool and every
screening and arbiter prompt and response). `<data>/discovery/INDEX.md`
catalogues runs.
