---
name: retrieve
description: Obtain PDFs for library items that lack one - download the version of record from each publisher's own site (a route per publisher, from the VU network), stage the files in a retrieval run, and attach them to Zotero only when the user says so. No preprint PDFs. Invoke on explicit request (/scriptorium:retrieve (or /retrieve), "get the missing PDFs") or when another spoke reports PDF-less additions and the user asks to fill them.
---

# retrieve

Fill in missing PDFs for existing library items in two separate steps:
**fetch** downloads and checks files, **attach** puts them into Zotero.
Fetching never writes to Zotero; attaching happens only on the user's
explicit go, usually for a whole staged run at once. Both steps read the
library live, so the Zotero app must be running; fetch also needs the VU
network, and attach also needs the daemon (with the Bridge plugin loaded in
Zotero).

## Rules

- **Version of record only**, from the publisher's site (or PubMed Central
  where the publisher deposits it). No preprint PDFs: preprint items are
  never targets.
- **Honest requests.** The fetcher identifies itself as scriptorium with a
  contact address, never as a browser. When a publisher blocks automated
  access, the block is reported, and the answer is to ask the publisher,
  never to work round it. No bot-evasion tools.
- **VU network.** `fetch_pdfs.py` refuses to start off the VU network
  (on campus or the VU VPN); the university's subscriptions depend on it.
- **Download first, attach later.** Never attach as you go.

## 1 — Plan

```
uv run python scripts/fetch_pdfs.py --plan
```

Counts the targets per publisher folder: regular items other than
preprints, with no PDF and no `own-work` tag, whose publisher folder has a
route in `scriptorium.pdf_routes` and which have a DOI (or, on routes that
read the item's URL, a URL). Publishers without a route are listed as
unrouted; adding a route is a code change. Present the counts and agree
the scope with the user before fetching.

## 2 — Fetch

```
uv run python scripts/fetch_pdfs.py [--pilot N] [--publisher NAME ...]
    [--limit N] [--skip-staged <run folder> ...] [--run <run folder>]
```

Start a new publisher or route with `--pilot N` (about N items spread over
the publishers) and look at the result before a full run. `--publisher`
limits the run to named publisher folders (exact Zotero folder names).
`--skip-staged` leaves out items whose PDF an earlier, not yet attached run
already holds; pass every such run. `--run` resumes a run, skipping items
already fetched. Long runs are launched detached (`nohup ... &`) so they
survive the session; anything that keeps the Mac awake (`caffeinate`) needs
the user's consent first.

Each run is a retrieval run (`<data>/retrieval/runs/<run>/`). A PDF that
passes the check (a real PDF, above a minimum size, the item's title on
its first pages) goes to `pdfs/<item key>.pdf`; one whose title check fails
goes to `pdfs-review/` for the user to look at. `fetch.jsonl` records one
line per item; `report.md` gives counts per publisher, the title-match
rate and one sample PDF per publisher for the user to open. A host that
blocks five times in a row is stopped for the run.

## 3 — Attach (only on the user's go)

```
uv run python scripts/attach_pdfs.py --run <run folder> [--batch 100] [--pause 20] [--limit N]
```

Attaches every PDF in the run's `pdfs/` (not `pdfs-review/`) through
`hub_attachment_link`, which names the file by citation key, stores it as a
linked file and indexes the full text. Each item is re-checked live first,
so nothing is attached twice; a re-run skips items already attached
(`attached.jsonl`). Attaching takes about 8 seconds per paper, so a large
run is an overnight job. The staged files are kept until the user has
checked the result.

## 4 — The rest

Papers without a route, behind a block, or held for review stay on the
`has_pdf: false` list (`hub_list_items` filter). The user may download
any of them with their own browser into `~/Desktop/to_file/`;
`/scriptorium:ingest` (mop-up mode) then attaches them. The dashboard's
missing-PDFs page gives a copyable list of links.

## 5 — Report

One block: run folder, targets, fetched (per publisher), title-match rate,
held for review, misses by kind (blocked, no URL, not found, HTML page),
and, after an attach, attached and indexed counts. Point the user to the
sample PDFs in `report.md`.
