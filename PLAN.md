# Scriptorium hub: implementation plan

**Goal:** build the hub of the collaborative version: server, client and Zotero exchange, running on the departmental server for a small test group.

**Architecture:** a FastAPI server over PostgreSQL with pgvector, one job worker that fills the stores, and a FastMCP client on each member's machine that is the only thing talking to the server. See the drawing and Table 1 in the spec.

**Stack:** Docker Compose, Python ≥ 3.12, uv, FastAPI, Uvicorn, psycopg 3, PostgreSQL 17 + pgvector, sentence-transformers (Qwen3-Embedding-0.6B, 256 dims), GROBID 0.9.1, FastMCP, httpx, Typer, pytest.

**Spec:** `README.md` on this branch. Executors read both.

## Global constraints

- One tool, one server request, same inputs; names and addresses as in Table 1; addresses under `/v1`.
- No reply to a member ever names a member.
- Every setting lives in the server's settings file or the client's settings file; none in code.
- Deterministic work is a script or job with a run folder (`run.json`, `report.md`, `work/`).
- The server keeps no per-member results or sets.
- The client package never imports the server package and has no heavy dependencies.
- Vectors are `halfvec(256)`, cosine. Passages are 20–300 words; artefacts are stored without a vector.
- No personal values, keys or model vendor names in the repository.
- Tests need no network, no GPU and no Zotero: outside sources, the embedder, GROBID and the plugin are faked behind the interfaces named below.
- A task is committed only when the whole test suite passes.

## Review focus

Each line is pinned by a test in the task named.

1. The same publication arrives twice at once (harvest and a member's lookup): one item results. Task 3.
2. A donated PDF is not the publication it claims to be: the job fails with a reason, the file is not kept, the member's progress shows it. Task 11.
3. The worker stops mid-job: on restart the job is waiting again and runs once. Task 5.
4. A client older than the server's API: every tool replies with one clear "update the client" message. Task 10.
5. Zotero closed or the plugin missing during add or donate: the tool says so and nothing is half-written. Task 14.

## Repository layout

```
server/   pyproject.toml, src/scriptorium_server/, tests/
client/   pyproject.toml, src/scriptorium_client/, tests/
zotero-bridge/
api/openapi.json     the contract: written by a server test, checked by a client test
README.md  PLAN.md  LICENSE  LICENSES/  CITATION.cff  docs/assets/
```

Server modules: `settings`, `db` (+ `schema.sql`), `items`, `identity`, `embedding`, `jobs`, `sources/{crossref,openalex,abstracts}`, `intake`, `tei`, `grobid`, `fulltext`, `retrieval`, `files`, `auth`, `api`, `admin`, `backup`.
Client modules: `settings`, `http`, `results`, `tools`, `zotero`, `cli`.

## Deployment

Docker Compose (`compose.yaml`, values in `.env`; steps in `docs/deploy.md`). Containers: `db` (`pgvector/pgvector:0.8.7-pg17`), `cli` (the admin command, one-off: `docker compose run --rm cli <command>`), and, as their tasks land, `server` (Task 9), `worker` and `grobid` (`grobid/grobid:0.9.1-crf`, Task 11), all but `db` and `grobid` from one image (`server/Dockerfile`). GPU access for `server` and `worker` is added at Deploy 2.

| What | On the host | In the containers |
|---|---|---|
| Code, `compose.yaml`, `.env` (database password) | `/opt/scriptorium` | image |
| Database files (copy-on-write off) | `/srv/scriptorium/postgres` | `/var/lib/postgresql/data` |
| PDFs, holding area, parsed texts, run folders, backups, model | `/srv/scriptorium/data` | `/srv/scriptorium` |
| Settings | `/srv/scriptorium/settings/settings.toml` | `/etc/scriptorium/settings.toml` |
| Runs as | the deploying account's uid in the containers; no system account | |
| Web address | an entry in the server's Caddy, VU addresses only, to the `server` container on localhost | |

The database address comes from `SCRIPTORIUM_DATABASE_ADDRESS` (set by Compose from `.env`). Development and tests use a local PostgreSQL, not the containers.

---|---|
| Code and Python environments | `/opt/scriptorium` |
| Data: PDFs, holding area, run folders, backups | `/srv/scriptorium` |
| Settings | `/etc/scriptorium/settings.toml` |
| Database files | PostgreSQL's default location |
| Runs as | system account `scriptorium`, no login |
| Web address | an entry in the server's Caddy, VU addresses only |

---

## Step 0: preparation

### Task 0: restructure the branch

- Merge `main` into `collaborative` (keeps this branch's README).
- Move into `server/src/scriptorium_server/`, as copies to develop freely: `tei.py`, `grobid.py`, `parsing.py` → `fulltext.py` (`parse_pdf(item_key, pdf_path, content_hash, store, grobid)`), `apis.py` and `abstracts.py` → `sources/` (`apis.configure(contact_email, s2_api_key)` replaces the settings lookup), `versions.py` + `identifiers.py` + `names.py` → `identity.py`, `runs.py` → `jobs.py` (`new_run(runs_dir, kind, slug)`, `finish_run`), the embedder of `index.py` → `embedding.py` (`MODEL`, `DIM`), `citekeys.py`. Their tests move with them.
- Not moved: BibTeX. The personal version takes entries from Zotero's exporter; Task 9 writes the server's own.
- Delete everything else of the personal version from this branch: `app/`, `launchd/`, `skills/`, `.claude-plugin/`, `.mcp.json`, `scripts/` except `check_push.sh`, and the remaining modules and tests. They stay on `main`.
- Two `pyproject.toml` files. Server script: `scriptorium`. Client script: `scriptorium-client`.
- Done when: `uv run --directory server pytest` and `uv run --directory client pytest` both pass with the moved tests.

Development needs PostgreSQL 17 with pgvector on the development machine and a database `scriptorium_test`.

## Step 1: stores, settings, admin command, seed import

### Task 1: server settings

- `settings.py`: `load(path) -> Settings`, `show() -> list[(name, value, explanation)]`, `set_value(name, value)` with validation. TOML file holding only non-default values; path from `SCRIPTORIUM_SETTINGS`. Secret fields print as `set` / `not set`.
- Settings defined here: database address, data folder, surveillance day and hour, backup folder and days kept, per-request maximum, rate per member per minute, embedding batch size, GROBID address, outside-source keys, contact email for outside sources.
- Tests: `test_set_rejects_unknown_name`, `test_set_rejects_bad_value`, `test_show_masks_secrets`, `test_every_setting_has_an_explanation`.

### Task 2: database

- `migrations/0001_schema.sql` and `db.py`: `connect(address=None, schema=None)`, `migrate(conn)` (numbered files, applied once each). Tests take the `db` fixture: a migrated schema of their own in `scriptorium_test`.
- Tables: `items` (key, citation_key, type, title, title_norm, abstract, year, date, venue_id, publisher, category, doi, arxiv_id, other_ids, creators, data, provenance_kind, provenance_member, added_at, has_fulltext, embedding, tsv), `item_creators` (item, name_key, position), `passages` (item, idx, part, pages, text, artefact, embedding, tsv), `venues` (name, issns, adapter, source_id, publisher, category, subscribed, needs_filter, marker), `members` (name, key_hash, created_at, revoked_at), `files` (item, sha256, bytes, donated_by, donated_at), `jobs` (kind, payload, status, member, created_at, started_at, finished_at, error, run_dir), `usage_log` (at, member, request, inputs, reply, status, duration_ms, client_version, agent).
- Unique: `items.doi` and `items.arxiv_id` (lower-cased), `files.sha256`. Indexes: HNSW on both `embedding` columns, GIN on both `tsv` columns.
- Tests: `test_migrate_is_repeatable`, `test_vector_extension_present`, `test_doi_is_unique_ignoring_case`.

### Task 3: items and identity

- `identity.py`: the personal version's rules, unchanged in behaviour.
- `items.py`: `admit(conn, record, provenance_kind, member=None, *, relation=None, publisher=None, category=None) -> (key, created: bool)`; a record is a dict in Zotero's field names; also `holder(conn, identifiers)`, `get(conn, key)`, `to_record(row)`. Applies identity: an existing DOI or arXiv ID returns the existing item; a preprint of a held published item is recorded on it; a published version arriving after its preprint replaces it in place (same key, citation key and provenance; vector emptied) and keeps the preprint's identifier.
- Tests: the moved version tests, plus `test_admit_twice_gives_one_item`, `test_two_sessions_admitting_at_once_give_one_item` (review focus 1), `test_preprint_identifier_finds_published_item`, `test_provenance_kind_is_kept_from_first_admission`.

### Task 4: embedding

- `embedding.py`: `class Embedder` with `embed(texts)` and `embed_query(text, kind)`; `RealEmbedder` (model, GPU when present), `FakeEmbedder` (deterministic, word-sensitive, for tests). `embed_pending_items(conn, embedder, batch_size)`, `embed_pending_passages(...)` fill empty vectors in batches; `check_revision` refuses a model revision other than the one recorded in table `meta`. Passages keep `embed_text` (the text without citation markers) where it differs.
- Tests: `test_pending_items_get_vectors`, `test_artefact_passages_get_no_vector`, and one test marked `model` comparing the real embedder with the stored reference vectors.

### Task 5: jobs

- `jobs.py`: `enqueue(conn, kind, payload, member=None) -> id`, `claim(conn, runs_dir) -> Job | None` (one at a time, safe with several workers), `finish(conn, job, error=None, summary=None)`, `requeue_unfinished(conn)` at worker start, `run_worker(conn, runs_dir, handlers, poll_seconds, until_idle, stop)`; a handler is `(conn, job) -> summary` and must be safe to run twice; each job gets a run folder.
- Tests: `test_claim_gives_each_job_once`, `test_unfinished_job_runs_again_after_restart` (review focus 3), `test_failed_job_keeps_its_error_and_run_folder`.

### Task 6: seed import

The starting collection is the first member's existing library, taken in full from the personal version.

- Two inputs. The personal index (`index.sqlite`): items, folder placement, item vectors, passages and passage vectors. A records file: each item's full Zotero record (the index lacks volume, issue, pages, ISSN, exact date, structured creators), written on the member's machine by `scriptorium seed-export <file>` from Zotero's local API, read-only, without tags, collections or notes.
- `seed.py`, `scriptorium seed <index.sqlite> --member <name> [--records <file>]`: every item is created as it is (`items.insert`, no search for other versions) with provenance "donation"; category, publisher and venue come from its folder path; item vectors, passages (text, pages, part, artefact mark) and passage vectors are copied unchanged, since model and passage rules are the same. An item already present is skipped; one whose identifier another item holds is skipped and listed. An item missing from the records file is built from the index. Each full text's PDF hash (MD5) goes to `pending_files`. Run folder with report.
- `scriptorium seed-files <folder>` moves to Task 11, where the file store and the stripping of annotations are built.
- Tests with a small fixture index: `test_seed_counts_match_source`, `test_seed_copies_vectors_unchanged`, `test_seed_twice_changes_nothing`, `test_seed_imports_no_tags_or_notes`, `test_export_writes_full_records_without_tags_collections_or_notes`.

### Task 7: admin command

- `admin.py` (Typer, script `scriptorium`): `members add|list|revoke`, `venues add|list|remove`, `jobs list|show`, `settings show|set`, `migrate`, `worker` (`serve` comes with Task 9). `members add` prints the key once and stores its hash. `members.py`: `add`, `authenticate(conn, key) -> member | None`, `revoke`, `listing`. Job handlers register in `admin.HANDLERS`.
- Tests: `test_member_key_is_shown_once_and_not_stored`, `test_revoked_key_is_refused`, `test_jobs_list_shows_failures`.

**Deploy 1:** the `db` container, the image, the schema. Load the seed.

## Step 2: authentication, API, retrieval, limits; client tools

### Task 8: authentication, usage log, limits

- `auth.py`: `handle(connect, request, name, inputs, run, per_minute)` wraps every request (key as `Authorization: Bearer`, client version and agent as `X-Scriptorium-Client` / `X-Scriptorium-Agent`; 426 for a client older than `MIN_CLIENT`). As planned: API key → member, else 401; rate per member, else 429 naming the limit; writes one `usage_log` row per request with inputs and the full reply. The client's version and agent name arrive as headers. No network address is stored.
- Tests: `test_unknown_key_is_refused`, `test_log_row_holds_inputs_and_full_reply`, `test_rate_limit_names_itself`, `test_log_holds_no_address`, `test_health_needs_no_key`.

### Task 9: retrieval and the API

- `retrieval.py`: `lookup`, `list_items`, `search_items`, `search_passages` (returns nothing until Step 3 fills passages), `item`, `facets`, `bibtex`. Filters: author, year, venue, publisher, category, type, has full text, added since, provenance, item keys (a set sent back). The two searches also take an exact phrase. The caller states count, fields and order; the per-request maximum is the only cap.
- `lookup` of a DOI or arXiv ID not held: fetch from the registry, verify, `admit(..., "activity", member)`, return it. Registry unreachable or identifier unknown: say so, admit nothing.
- `api.py`: `create_app(connect, embedder, settings, registry)`; the routes of Table 1 plus `/v1/health`; find requests are POST with a JSON body, unknown inputs refused; a test writes `api/openapi.json` (with `SCRIPTORIUM_WRITE_OPENAPI=1`) and fails when it is out of date. `sources/crossref.py`: `fetch(kind, value)` asks Crossref for a DOI and DataCite for an arXiv ID. `scriptorium serve --host --port` (default 127.0.0.1:8765). Vector searches pass the query vector as a value and use pgvector's iterative scan; a search within `item_keys` is exact.
- Tests: `test_filters_combine_with_and`, `test_author_matches_any_name_form`, `test_search_within_item_keys_is_exact`, `test_lookup_of_unknown_doi_admits_it_once`, `test_lookup_with_registry_down_admits_nothing`, `test_reply_never_contains_a_member_name`, `test_request_over_maximum_names_the_limit`, `test_openapi_file_is_current`.

### Task 10: client core

- `settings.py`: file in the user's config folder, readable by that user only; `cli.py`: `scriptorium-client setup` (server address, API key), `scriptorium-client serve` (the MCP server).
- `http.py`: one function per server request; sends key, client version, agent name; turns "client too old", 401, 429 and unreachable into plain messages.
- `results.py`: `fit(reply, budget)` decides what enters the tool reply; a larger result is written whole to a file in the client's results folder, and the tool returns path, count and first page. Set tools: `set_read(path, page)`, `set_mark(path, keys, included)`, `set_export_bibtex(path)`.
- `tools.py`: class `Tools` (methods are the tools; `build_server()` registers them with FastMCP and reads the agent's name from the MCP session); find tools take `within_set` (a saved result's path, sent as the `item_keys` filter); the find and inspect tools of Table 1; `status` combines `/v1/health` with client version and whether Zotero and the plugin answer.
- Tests against a fake server: `test_each_tool_sends_the_request_in_the_contract`, `test_large_result_goes_to_a_file`, `test_set_file_round_trip`, `test_old_client_gets_one_clear_message` (review focus 4), `test_settings_file_is_private`, `test_status_reports_all_four_facts`.

**Deploy 2:** the API behind the server's front door, VU addresses only. First member keys. Check from a member's machine on each operating system: setup, status, a search.

## Step 3: files, full-text job, passage search

### Task 11: full-text job

- `scriptorium seed-files <folder>`: stores each PDF whose MD5 is in `pending_files`, stripped like a donation; other files are left alone. Test: `test_seed_files_stores_only_known_hashes`.
- `grobid.py` becomes a client of the GROBID address in the settings (no local install to start and stop).
- `fulltext.py`: `process(donation)`: parse with GROBID; check the parsed title and authors against the item (or, for an identifier not held, admit it first); on mismatch fail with the reason and delete the held file; strip annotations and file author details; cut passages by the existing rules; store text, then embed; move the PDF to the file store by content hash; set `has_fulltext`.
- Tests with stored TEI: the moved passage tests, `test_wrong_document_fails_and_is_not_kept` (review focus 2), `test_annotations_are_stripped`, `test_same_file_twice_is_stored_once`, `test_donation_outside_collection_creates_item`.

### Task 12: files on both sides

- Server `files.py`: `POST /v1/files` (PDF and identifier → holding area and a job; replies at once), `GET /v1/files/{key}`, `GET /v1/files/held` (identifiers with full text), `GET /v1/donations` (the member's own: waiting, done, failed with reason).
- Client tools: download PDF (to the client's PDF folder), donate PDF (one file or a folder; skips what `/held` lists), donation progress.
- Tests: `test_upload_replies_before_processing`, `test_progress_shows_only_own_donations`, `test_donate_folder_skips_held`, `test_download_saves_the_same_bytes`, `test_passage_search_finds_donated_text`.

**Deploy 3:** the `worker` and `grobid` containers; donate a handful of PDFs end to end.

## Step 4: Zotero exchange

### Task 13: plugin read functions

- `zotero-bridge` 0.1.9: `items/identifiers` (DOIs and arXiv IDs of the member's items, nothing else), `attachments/pdfs` (for chosen collections or the whole library: item identifier and file path of each PDF).
- Checked by hand in Zotero on one machine per operating system; recorded in the task's commit message.

### Task 14: client Zotero tools

- `zotero.py`: `add_items(keys)`: fetch each item and its PDF where held, skip identifiers already in Zotero, create through the plugin, attach the PDF by the member's Zotero file settings. `donate_pdfs(scope)`: list PDFs through the plugin, compare with `/held` locally, show the count, upload on confirmation.
- Tests against a fake plugin: `test_add_skips_items_already_present`, `test_add_never_updates_or_deletes`, `test_donate_sends_only_pdf_and_identifier`, `test_donate_compares_locally`, `test_zotero_closed_says_so_and_writes_nothing` (review focus 5).

**Deploy 4:** the plugin as a release file; one member per operating system adds items and donates.

## Step 5: backup

### Task 15: backup and restore

- `backup.py`, `scriptorium backup` and a nightly timer: database dump and PDFs added since the last run, to the backup folder, older runs removed after the days kept.
- `scriptorium restore <run>` into an empty database and file store.
- Tests: `test_backup_then_restore_gives_the_same_counts`, `test_old_backups_are_removed`. On the server: one real restore into a scratch database before members rely on it.

## Step 6: intake job

### Task 16: sources and the intake job

- `sources/`: `class Source` with `harvest(venue, since) -> iterator of records` in one common item form; `Crossref`, `OpenAlex`; `abstracts.fill(record)` trying Crossref, OpenAlex, Semantic Scholar, Scopus, Springer in order; an empty abstract counts as missing.
- `intake.py`: `run_intake(venue_ids=None, since=None)`: for each subscribed venue not marked `needs_filter`, harvest by index date from the venue's marker, admit, fill abstracts, embed, advance the marker. Venues marked `needs_filter` are skipped until the filter exists.
- Tests with recorded replies: `test_harvest_pages_until_exhausted`, `test_marker_advances_only_after_success`, `test_rerun_admits_nothing_new`, `test_needs_filter_venue_is_skipped`, `test_abstract_chain_stops_at_first_hit`.
- The outside sources' current access terms are checked before this task starts.
- Admin command gains `backfill [--venue] [--from]`; `venues add` enqueues that venue's backfill. Test: `test_venues_add_enqueues_backfill`.

**Deploy 6:** the weekly surveillance timer. Subscribe the first venues and run their backfill. Data now accumulates for the intake-filter question.

---

After the hub: workflows, then collaborative features, then the rest, each with its own plan.
