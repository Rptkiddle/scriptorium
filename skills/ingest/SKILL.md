---
name: ingest
description: Add papers to the Zotero library through the scriptorium hub, with verified metadata, correct collection placement, and PDFs stored as linked files. Invoke on explicit request — /scriptorium:ingest (or /ingest), "ingest", "add papers to Zotero", "process papers" — or when another spoke (discovery, surveillance) hands over verified candidates and the user confirms ingestion.
---

# ingest

Add items to the Zotero library via the scriptorium hub (daemon at
`http://localhost:8765`; tools `hub_lookup`, `hub_search_items`, `hub_item`,
`hub_collections`, `hub_collection_create`, `hub_item_create`,
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

Every ingest is a run. Before anything else, from the repo
(`~/Desktop/code/1_SELF/scriptorium/scriptorium`), run
`uv run python -m scriptorium.runs new ingestion`, which prints the run
folder (`<rundir>`, named `<YYYY-MM-DD-HHMMSS>`). Keep the run's working
files (entries, verification notes) in `<rundir>/work/`, and pass
`run: "ingestion/<run name>"` to every hub write tool (`hub_item_create`,
`hub_collection_create`, `hub_attachment_link`, `hub_item_update`,
`hub_item_tags`, `hub_item_collections`, `hub_note_create`,
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
- **Item JSON is complete at creation:** correct `itemType` (preprints
  must be created as `preprint` — the type cannot be changed after), full
  creator list, date, DOI, `abstractNote`, venue fields. If external
  sources show a published version superseding a preprint (or vice versa),
  flag it and let the user choose the version.
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

Reference mode (Crossref-originated candidates): write one entries JSON
file to `<rundir>/work/entries_in.json` — a list of `{"doi",
"collection_key", "tags": [...], "extra": "..."}` with any fallback
metadata you hold (title, authors, venue, year) — and run

```
uv run python scripts/ingest_add.py --entries <rundir>/work/entries_in.json --run <rundir>
```

from the repo. It writes `<rundir>/added.json`. The script is the deterministic tail shared with the
surveillance add step: it dedupes against the library, fetches the full
Crossref record per DOI, and creates through the bridge with generated
citation keys. Relay its printed summary.

Other sources (metadata assembled or corrected by hand): `hub_item_create`
with the verified JSON and confirmed `collection_key`, one call per paper
(or one batch call). Citation keys are generated by the hub and returned.
Either way the items are immediately usable — proceed straight to
attaching.

## Step 4 — PDF (inbox and mop-up modes only)

Reference mode creates items metadata-only: PDF acquisition is a separate
concern, handled by the `/scriptorium:retrieve` skill (publisher downloads staged
for a later attach, plus the user's browser for the rest). Simply note in the report that the new items are on
the `has_pdf: false` list.

In inbox and mop-up modes the file is on disk already. Attach with
`hub_attachment_link(parent_key, pdf_path)` — omit the filename; the hub
names the file by the parent's citation key. The hub copies the file into
the linked-attachments folder and full-text-indexes the paper in the same
call (the result carries `"indexed"`). Delete the inbox copy only after
the result confirms the copied file and that indexing succeeded or was
deferred (`"deferred"`: a sync was running and the next sync indexes the
paper); keep it if `"indexed"` holds an error.

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
Flagged: <duplicates skipped, version questions, retractions, or none>
```

## Maintenance notes

- `hub_status` first if anything seems off (Zotero reachability, failures).
- Items missing PDFs at any time: `hub_list_items` with
  `{"has_pdf": false}` (its `total` is the count).
- Write operations need Zotero running with the Scriptorium Bridge plugin
  installed; a clear error says so if either is missing.
