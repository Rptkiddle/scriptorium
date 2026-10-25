---
name: ingest
description: Add papers to the Zotero library through the scriptorium hub, with verified metadata, correct collection placement, and PDFs stored as linked files. Invoke on explicit request — /scriptorium:ingest (or /ingest), "ingest", "add papers to Zotero", "process papers" — or when another spoke (discovery, surveillance) hands over verified candidates and the user confirms ingestion.
---

# ingest

Add items to the Zotero library via the scriptorium hub (daemon at
`http://localhost:8765`; tools `hub_lookup`, `hub_search_items`, `hub_item`,
`hub_collections`, `hub_collection_create`,
`hub_attachment_link`, `hub_index`, `hub_status`). Metadata is always
externally verified before anything is created; PDFs are stored as linked
files (the hub copies them into the linked-attachments folder). If the
daemon is unreachable, tell the user to start it and stop.

Three modes, chosen by what the input is:

- **Inbox mode** — PDFs waiting in `~/Desktop/to_file/` (the default when
  invoked with no arguments). Metadata is derived from the PDFs, verified,
  and items are created with the files attached. Read and delete access in
  that directory is pre-authorized for this skill.
- **Reference mode** — verified metadata handed over (e.g. from a discovery
  run) with no files. Items are created metadata-only (with the right item
  type: journal article, conference paper, preprint, book section, book);
  PDFs are left to /scriptorium:retrieve.
- **Mop-up mode** — the user has obtained PDFs for items that already exist
  without one. Match files to items, attach, index. Nothing is created.

Process papers sequentially but batch the user gates: one duplicate
confirmation round, one collection confirmation round, one closing report.
Track the item key returned by every create — all later calls depend on it.

**A preview only** ("what would happen if I added these?", "don't add
anything yet"): write the entries (`[{"doi": "..."}, ...]`, or `"item"`
entries) to a temporary file (any scratch location, e.g. `/tmp`) and run,
from the repo,
`uv run python scripts/ingest_add.py --entries <file> --dry-run` without
`--run`. It writes nothing anywhere (no run folder) and prints what would
be created, what is already in the library, and every preprint/published
version match. No run is needed for a preview.

Every ingest that adds papers is a run. Before anything else, from the repo
(`~/Desktop/code/1_SELF/scriptorium/scriptorium`), run
`uv run python -m scriptorium.runs new ingestion`, which prints the run
folder (`<rundir>`, named `<YYYY-MM-DD-HHMMSS>`). Keep the run's working
files (entries, verification notes) in `<rundir>/work/`, and pass
`run: "ingestion/<run name>"` to every hub write tool
(`hub_collection_create`, `hub_attachment_link`, `hub_item_update`,
`hub_item_tags`, `hub_item_collections`, `hub_note_create`, `hub_item_type`,
`hub_item_delete`), so the write log attributes each change to this run.

## Common rules

- **Verification precedes creation, always.** Cross-check via Crossref
  (authoritative for bibliographic fields: title, authors, date, venue,
  volume/issue/pages, DOI) and Semantic Scholar (supplements: abstract,
  publication type). Both must agree on title, authors, year, venue, and
  DOI before an item is created — EXCEPT when the candidate originates
  from Crossref by DOI (surveillance handoffs): there Crossref alone
  suffices, since Semantic Scholar's index lags fresh papers by days to
  weeks and the record cannot be fabricated. Report retraction notices
  Crossref surfaces.
- **Duplicate check before every create:** `hub_lookup` with every
  candidate's DOI (or arXiv ID, or title when there is no identifier), in
  one batch call. It also finds a preprint whose published version is
  already in the library ("recorded preprint of this item"). For a title
  with no exact match, check the candidates it lists. Likely duplicates go
  to the user in one batch — never silently skip or create over one.
- **A published paper is kept as its published version only.** Its
  preprints are recorded in the published item's Extra
  (`SocArXiv: 10.31235/osf.io/abcde`, `arXiv: 2603.13270`). Two items are
  versions of one paper when Crossref links them (`is-preprint-of`,
  `has-preprint`) or when their titles and first authors match and one of
  them is a preprint. `scripts/ingest_add.py` applies the rule itself: a
  preprint of a paper already in the library is not created but recorded
  on it; a published paper whose preprint is in the library is created,
  and the preprint is moved to the trash unless it has attachments or
  notes, or nothing to record it by (then it is kept and reported). Before
  the duplicate round, run it with `--dry-run` (writes nothing; entries
  need only `doi` or `item` at that point) and show each match it lists as
  what will happen; run it for real only after confirmation. A DOI that is
  recorded as a preprint on an item counts as already in the library (the
  script reports it as "already in library"). The same rule
  is applied across the whole library by `scripts/dedupe_versions.py`
  (a plan first; `--apply <run folder>` only on the user's yes).
- **Item JSON is complete at creation:** correct `itemType` (preprints
  must be created as `preprint`; `hub_item_type` can change it later, but
  moves fields the new type lacks into Extra), full
  creator list, date, DOI, `abstractNote`, venue fields.
- **Writes are local and instant.** They go through the bridge plugin in
  the running Zotero, so created items and attachments are immediately
  visible and usable — never wait, poll, or sleep after a write. If a
  write fails because Zotero or the plugin is unavailable, stop and relay
  the error to the user; there is no fallback path.

## Step 1 — Gather and verify

Inbox mode: list `~/Desktop/to_file/*.pdf`; none → report and exit.
Identify DOI-first, cheapest signal first: (1) a DOI pattern in the
filename (publisher downloads often are one, e.g. `s41599-026-07316-7`);
(2) a DOI in the text (`pdftotext <file> - -l 2 | grep -ioE
'10\.[0-9]{4,}[^ "]*'`); (3) only then title/author extraction from the
first page (`pdftotext <file> - -l 1`) and search by title. A found DOI
resolves the whole record in one Crossref call. Then verify per the
common rules. Reference mode: take the handed-over records; they arrive
verified, so only fill gaps (abstract, open-access link). Mop-up mode:
list the inbox PDFs and match each against PDF-less items
(`hub_search_items` with `{"has_pdf": false}`); present the matching, then
go to Step 4.

## Step 2 — Duplicates and collections (user gates, batched)

Run the duplicate check for every candidate; present any hits in one
round. Then propose collection placement for each paper within the
existing hierarchy (publication type > publisher > venue, the venue under
its current publisher): find targets with `hub_collections`, propose
`hub_collection_create` where a venue or publisher folder is missing, and
present all placements in one round for confirmation.

## Step 3 — Create

Every paper goes through one script. Write one entries JSON file to
`<rundir>/work/entries_in.json`: a list of entries, each with
`"collection_key"`, `"tags": [...]` and `"extra": "..."`, plus either
- `"doi"` for a paper with a Crossref DOI (reference mode, inbox PDFs whose
  DOI was found), with any fallback metadata you hold (title, authors,
  venue, year); or
- `"item"`: complete Zotero item JSON for a paper without one (an arXiv
  preprint: `itemType` `preprint`, `repository` `arXiv`, `archiveID`
  `arXiv:<id>`, `DOI` `10.48550/arXiv.<id>`; a NeurIPS, PMLR or ACL paper
  known by its URL; anything assembled by hand). Types: journalArticle,
  conferencePaper, preprint, bookSection, book; a field the type cannot
  hold is refused, not dropped.

Then run

```
uv run python scripts/ingest_add.py --entries <rundir>/work/entries_in.json --run <rundir>
```

from the repo. It writes `<rundir>/added.json`, with outcomes keyed by
each entry's DOI (else its arXiv ID, URL or title). The script is the
deterministic tail shared with the surveillance add step: it dedupes
against the library (identifiers, and URL for item entries), fetches the
full Crossref record per DOI, applies the version rule above, and creates
through the bridge with generated citation keys. Relay its printed
summary. In inbox mode, attach each PDF (Step 4) to the key `added.json`
lists for it. When the dry run shows that an inbox PDF's paper is a
preprint of an item already in the library (so nothing will be created for
it), say so in the duplicate round and ask whether the PDF goes on that
item or stays in the inbox.

The items are immediately usable — proceed straight to attaching.

## Step 4 — PDF (inbox and mop-up modes only)

Reference mode creates items metadata-only: PDF acquisition is a separate
concern, handled by the `/scriptorium:retrieve` skill (publisher downloads staged
for a later attach, plus the user's browser for the rest). Note in the report
that the new items are on the `has_pdf: false` list, and that
`/scriptorium:retrieve` can fetch just these papers with
`scripts/fetch_pdfs.py --keys-from <rundir>/added.json`.

In inbox and mop-up modes the file is on disk already. Attach with
`hub_attachment_link(parent_key, pdf_path)` — omit the filename; the hub
names the file by the parent's citation key. The hub copies the file into
the linked-attachments folder and full-text-indexes the paper in the same
call (the result carries `"indexed"`). Delete the inbox copy only after
the result confirms the copied file and that indexing succeeded or was
deferred (`"deferred"`: a sync was running and the next sync indexes the
paper); keep it if `"indexed"` holds an error. The first attach after the
PDF parser (GROBID) has been idle starts it and takes about a minute;
later ones take seconds. Calling it again for the same PDF is harmless: it
returns the existing attachment (`"existing": true`) and writes nothing.

## Step 5 — Index and report

Attached papers are already indexed (Step 4); `hub_index` is needed only
for metadata-only items (catalogue entry). Then report in one block —
state the true end state; never report indexing as done when it failed.
Write the same block to `<rundir>/report.md`, then close the run:
`uv run python -m scriptorium.runs finish <rundir> --status ok --summary
"<the Created line>"` (`--status partial` when an item or attachment failed).

```
**ingest: report**
Created: <n> items — <key: short citation> ...
Attached: <n> PDFs (inbox/mop-up)
Indexed: <n> full-text · <n> catalogue-only
Metadata-only: <list, or none> — PDFs via /scriptorium:retrieve when convenient.
Versions: <preprints recorded on their published item; preprints replaced (trashed or kept), or none>
Flagged: <duplicates skipped, retractions, or none>
```

## Maintenance notes

- `hub_status` first if anything seems off (Zotero reachability, failures).
- Items missing PDFs at any time: `hub_list_items` with
  `{"has_pdf": false}` (its `total` is the count).
- Write operations need Zotero running with the Scriptorium Bridge plugin
  installed; a clear error says so if either is missing.
