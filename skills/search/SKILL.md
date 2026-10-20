---
name: search
description: Look things up in the user's Zotero library through the scriptorium hub - is a paper there (DOI, arXiv ID, title), everything by an author or in a venue or year, how many, what the library has on a topic, which passages say something, where a PDF is. Use for any "do I have / what do I have by, in, on..." question about the library. Not for citation-grounded answers (interrogate), adding papers (ingest), getting PDFs (retrieve) or searching beyond the library (discover).
---

# search

Spot searches over the library with the hub_* tools. Page sizes and k limits
are in each tool's description; a request over a limit gets an error naming it.

## 1 — Pick the tool by the question

| Question | Tool |
|---|---|
| Is this paper here? (DOI, arXiv ID, link, exact title; one or many) | `hub_lookup` |
| Everything matching filters: all by an author, a venue's papers for a year; how many | `hub_list_items` (`limit=0` = count; page with `next_offset`) |
| Papers ABOUT a topic, optionally narrowed by filters | `hub_search_items` (`abstracts=true` to judge relevance without opening items) |
| What papers SAY: passages, evidence, methods | `hub_search_chunks` (full text only: papers with an indexed PDF) |
| Breakdowns: items per year, venue, type; resolving names | `hub_facets` (`scope="items"`; `filters` to count within a set) |
| One item in full: all authors, abstract, PDF path, notes | `hub_item` (item or citation key) |
| The folder tree, or one folder's items | `hub_collections` |
| Tags | `hub_tags` |

`hub_search_pool` lists the full-text pool for the interrogate skill; it is not a search.

## 2 — Filters (item tools; full-text tools take the first eight)

`creators`, `creators_all`, `years`, `year_from`, `year_to`, `venue`, `citation_keys`,
`item_keys`, `item_types`, `categories`, `publishers`, `venue_folders`,
`added_since` ("YYYY-MM-DD"), `tags`, `collections`, `has_pdf`.

- Values within one filter match ANY; different filters must ALL hold.
- "By both X and Y" / "co-authored": `creators_all`, never two values in `creators`.
- List filters take lists even for one value: `{"creators": ["Trilling"]}`.
- Authors: any form works ("van Atteveldt", "Atteveldt, W.", "Wouter van Atteveldt").
- Venues: prefer `venue_folders`; `venue` is the raw field and needs the exact full name.

## 3 — Resolve before filtering

- Venue, publisher and category names: `hub_facets(field, contains=..., scope="items")`
  first. Folder names carry the usual acronym, e.g. "(ICWSM)", "(NeurIPS)".
- Years: check the range with `hub_facets("years", scope="items")`; a filter outside
  the library's stock returns nothing, without error.
- "Journal articles" is `item_types: ["journalArticle"]` or `categories: ["Journals"]`;
  the counts differ, so say which you used.
- Query text holds the topic only; who, where, when and what kind are filters.
  A full title also works as a query.

## 4 — Routines

- **Published version of a preprint:** `hub_lookup` with the arXiv ID or preprint
  DOI; a match marked "recorded preprint of this item" IS the published version.
- **Duplicate check before adding papers:** `hub_lookup` with every identifier at once.
- **Complete list:** `hub_list_items`; keep calling with `offset=next_offset` until it
  is null; report `total`.
- **Right author:** results show the first 10 authors with given names; for longer
  lists or a common surname, check with `hub_item`.

## 5 — Iterate

- Zero results: check each filter value with `hub_facets`; drop the narrowest filter;
  for a topic, rephrase with other terms.
- Too many: add filters (year, venue, type, `has_pdf`) or narrow the query.
- Full-text questions with few hits: most items have no indexed PDF; say so, and
  fall back to `hub_search_items` with `abstracts=true`.

## 6 — Report

- Give item keys or citation keys with each paper, and say which have no PDF.
- Counts come from `total` or facet counts, never from counting a page by hand.
- "Not in the library" only after `hub_lookup` found no match and its candidates
  were checked; a filtered or topical search that returns nothing proves nothing.
- Say which filters and folder names you used, so the user can see the scope.
