# Scriptorium collaborative: design spec

## 1. Purpose

Collaborative implementation for Scriptorium with remote + client split. Server is designed to maintain a large-scale collection and embed + index + serve it efficiently via agent-facing MCP tools. Client application (FastMCP) allows for vendor-free agent interaction with optional Zotero connector for two-way library exchange. Design is subject to change during development and testing.

## 2. Architecture

```
 CLIENT (member's machine)                     SERVER (departmental Linux server)
 macOS / Linux / Windows
                                               ┌──────────────────────────────────────────────────┐
┌────────────────────────────┐                 │ FRONT DOOR                                       │
│ AGENT                      │            ┌───►│ HTTPS reverse proxy · VU network addresses only  │
│ any agent that supports MCP│            │    └───────────────────────┬──────────────────────────┘
└─────────────┬──────────────┘            │                            │
              │ MCP, local                │    ┌───────────────────────┴──────────────────────────┐
┌─────────────┴──────────────┐            │    │ SCRIPTORIUM SERVER  (Python process, FastAPI)    │
│ SCRIPTORIUM CLIENT         │            │    │ ┌──────────────────────────────────────────────┐ │
│ a FastMCP server (Python)  │            │    │ │ AUTHENTICATION  API key → member · usage log │ │
│ · agent-facing tools       │◄───HTTPS───┘    │ └──────────────────────┬───────────────────────┘ │
│ · workflow instructions    │ web service     │ ┌──────────────────────┴───────────────────────┐ │
│ · PDF download and upload  │ + API key       │ │ API  one request per client tool (Table 1)   │ │
│ · Zotero exchange, when    │                 │ └──────────┬───────────────────────┬───────────┘ │
│   Zotero + plugin present  │                 │ ┌──────────┴──────────┐ ┌──────────┴───────────┐ │
└─────────────┬──────────────┘                 │ │ RETRIEVAL           │ │ FILES                │ │
              │ HTTP, localhost                │ │ exact lookup        │ │ receive donation     │ │
┌─────────────┴──────────────┐                 │ │ filter, count       │ │  → job queue         │ │
│ ZOTERO DESKTOP  (optional) │                 │ │ semantic: embed     │ │ serve PDF            │ │
│ + Scriptorium Bridge plugin│                 │ │  query, rank vectors│ │ list held PDFs       │ │
│ member's own library       │                 │ │ keyword match       │ │ donation progress    │ │
└────────────────────────────┘                 │ └──────────┬──────────┘ └──────────┬───────────┘ │
                                               └────────────┼───────────────────────┼─────────────┘
┌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┐                │ reads                 │ serves, queues
┊ FASTMCP SERVER: TOOLS AND WORKFLOWS      ┊   ┌────────────┴───────────────────────┴─────────────┐
┊ Tools (Table 1)                          ┊   │ STORES                                           │
┊ · find: lookup, list, search items,      ┊   │ ┌──────────────────────────────┐ ┌─────────────┐ │
┊   search passages                        ┊   │ │ DATABASE  PostgreSQL+pgvector│ │ FILE STORE  │ │
┊ · inspect: item, facets, BibTeX, status  ┊   │ │ items + abstracts · filters  │ │ disk        │ │
┊ · files: download PDF, donate PDF,       ┊   │ │ passages · keyword index     │ │ PDFs by     │ │
┊   donation progress                      ┊   │ │ vectors (items, passages)    │ │ content hash│ │
┊ · Zotero: add items, donate PDFs         ┊   │ │ members · usage log          │ │ parsed full │ │
┊ · workflow: return instructions          ┊   │ │ donations · job queue        │ │ texts       │ │
┊ Workflows (after the hub)                ┊   │ └──────────────────────────────┘ └─────────────┘ │
┊ · search (semantic retrieval)            ┊   └────────────────────────▲─────────────────────────┘
┊ · discovery (PRISMA-style review)        ┊                            │ takes jobs, writes results
┊ · interrogation (PaperQA2-style          ┊   ┌────────────────────────▼─────────────────────────┐
┊   summarization)                         ┊   │ JOBS  (one worker process, one queue, a run      │
└╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┘   │ folder per job)                                  │
                                               │ · intake: outside sources → megajournal filter   │
                                               │   (model) → items → embed title + abstract       │
                                               │ · full text: PDF → check against item → GROBID   │
                                               │   → passages → embed                             │
                                               │ Embedding: Qwen3-Embedding-0.6B (GPU)            │
                                               │ Started by: schedule (weekly surveillance), a    │
                                               │ donation, the admin command (one-off backfill)   │
                                               └───────┬──────────────────────────────────────────┘
                                                       │ HTTPS
                                               ┌───────┴──────────────────────────────────────────┐
                                               │ OUTSIDE SOURCES  Crossref (items) · OpenAlex,    │
                                               │ Scopus, Springer, Semantic Scholar (abstracts)   │
                                               │ · VU library (PDFs, TBC)                         │
                                               └──────────────────────────────────────────────────┘
                                               ┌──────────────────────────────────────────────────┐
                                               │ ADMIN COMMAND (on the server)  members, keys,    │
                                               │ subscribed venues, settings, job status          │
                                               └──────────────────────────────────────────────────┘
```

**Table 1. Client tools and corresponding server requests**

| Group | Tool | Server request | Acts on |
|---|---|---|---|
| find | lookup | `/v1/lookup` | Identifiers (DOI, arXiv ID), exact titles. An identifier not held is fetched, verified and added |
| find | list | `/v1/items` | Items by filters only; counts |
| find | search items | `/v1/search/items` | Title + abstract vectors, within filters |
| find | search passages | `/v1/search/passages` | Paragraph vectors of held full texts, within filters |
| inspect | item | `/v1/items/{key}` | One item in full |
| inspect | facets | `/v1/facets` | Values a filter can take, with counts |
| inspect | BibTeX | `/v1/bibtex` | Chosen items, formatted |
| inspect | status | `/v1/health` | Server reachable, collection size; plus client version and Zotero found (local) |
| files | download PDF | `/v1/files/{key}` | Held PDF, saved to disk |
| files | donate PDF | `/v1/files` | PDF + DOI or arXiv ID, uploaded |
| files | donation progress | `/v1/donations` | The member's donations: waiting, done, failed |
| Zotero | add items | `/v1/items/{key}`, `/v1/files/{key}` | Fetched, then written to Zotero locally |
| Zotero | donate PDFs | `/v1/files/held`, `/v1/files` | Client fetches the list of held PDFs, compares locally, sends only PDFs the server lacks |
| workflow | workflow | none (local) | Returns the instruction text |

Filters (find tools): author, year, venue, publisher, category, type, has full text, added since, provenance. The two search tools also take an exact phrase (keyword index).

## 3. Server

- **Collection.** One item per publication, with abstract. 100k–500k items, about one in five with a PDF. Read-only for members.
  - Sources: subscribed venues (journals, proceedings, preprint servers; 2020 onwards); members' donations from any venue; any publication a member's activity leads to (catch-all), admitted once its identifier is verified at the registry.
  - Provenance per item, usable as a filter. Members see its kind only (subscribed venue, donation, member activity).
  - Identity: one item per publication, matched by DOI or arXiv ID, else title and authors. Published version only; a preprint's identifier is recorded on the published item and finds it.
- **Web service.** FastAPI under Uvicorn, behind the server's HTTPS front door, which admits VU network addresses only.
  - Authentication and usage logging in one place, before any request is handled.
  - API: one request per client tool (Table 1). Nothing is served without an API key. Addresses versioned (`/v1`); an outdated client is told to update.
  - The server serves what the client asks for: how many results, which fields, in what order. It keeps no per-member results or sets.
  - Two protective limits: a maximum per request and a rate per member. A refused request says which limit it hit.
  - A donation is queued: the PDF to a holding area, an entry to the job queue.
- **Database.** PostgreSQL with pgvector, run as a server: tables, keyword search and vector search in one place. Revisited only if latency or other trouble appears.
- **Jobs.** The background layer that fills the stores. One worker process takes entries from the job queue and writes results back; a run folder per job.
  - Intake job: harvest, filter (open question), store items, fill abstracts, embed title + abstract. Backfill once, at setup; weekly surveillance after. One harvester for both.
    - Sources are adapters returning one item form; each subscribed venue names its adapter. Crossref API for journals; OpenAlex for venues Crossref lacks.
    - Abstracts, in order: Crossref, OpenAlex, Semantic Scholar, Scopus, Springer.
    - Harvest by index date, with a marker per venue.
  - Full-text job: parse (GROBID), check the PDF against its item (DOI on the first page, or title), strip annotations and file metadata, cut into paragraph passages (references, captions and tables excluded; publishers' download stamps removed), store, embed. A PDF outside the collection creates a verified item. A rejected PDF is deleted, with the reason in the member's donation progress. In the hub, PDFs arrive by donation only.
- **Backup.** Nightly, Scriptorium data only: database dump and new PDFs, kept for a set number of days, to a separate folder on the server; off-server storage when available. Restore tested once before members rely on it.
- **Admin command.** Members and keys; subscribed venues (adding one backfills it); job status and failures; settings.
- **Settings.** One readable file, shown and changed through the admin command, each setting with a one-line explanation. Keys for outside sources shown as set or not set. Venues and members are data, not settings.
- **Deployment.** Docker Compose: `db` (PostgreSQL with pgvector), `server` and `worker` (one Scriptorium image), `grobid` (GROBID's image). All data in one folder on the host, mounted into the containers; the admin command runs in a container. Steps: `docs/deploy.md`.

## 4. Client

- **Agent.** Any agent that supports MCP. Talks only to the client. Nothing else is assumed about it.
- **Client.** Required. Local MCP program; the only thing that talks to the server.
  - Tools: one tool, one server request, same inputs (Table 1).
  - Results: the client decides what the agent sees. What fits goes into the reply; a large result is saved whole to a file on the member's machine, and the agent gets the path, the count and the first page.
  - Sets: a set is such a local file. Client tools read it in pages, mark items included or excluded, export BibTeX, and send its item keys as a filter with a later request.
  - Zotero exchange, active when Zotero and the Bridge plugin are present. All reading and writing goes through the plugin, which reads only identifiers held and PDFs with their identifiers.
    - Add: chosen items into the member's Zotero, with PDFs where held. Skips items already present. Never edits or removes.
    - Donate: PDFs from the member's library, part or all.
  - Without Zotero: BibTeX file, PDFs saved to a folder, donation from a folder.
  - Workflow instructions (after the hub): held in the client, returned to the agent by a tool. A workflow is these instructions plus server retrieval; the agent does all judging.
- **Settings.** Made in the terminal, through the client. No web pages or panels, on either side.
- **Repository.** One repo, three folders: server, client, Zotero plugin. Built on the `collaborative` branch; `main` stays the personal version until the switch. Parsing, passage rules, embedding, source fetching and identity rules start as copies from the personal version; nothing is shared with it. The client has its own short dependency list.
- **Distribution.** GitHub only. Client fetched from the repo by the agent's settings entry; plugin as a release file.
- **Setup.** (1) Install `uv`. (2) Add the client to the agent, one command. (3) Run the client's `setup`: server address and API key, stored in the client's settings file, readable by that user only. (4) Zotero users: install the plugin file in Zotero. Off campus, the university VPN must be connected.

## 5. Collaborative features

Planned after the hub and the workflows.

## 6. Users and data

- One API key per member, created and revoked through the admin command; shown once; stored hashed.
- A donation sends the PDF and its DOI or arXiv ID only. Donor noted per file.
- Member names are visible to administrators only, through the admin command. No tool reply names a member.
- Usage log: maximal. One entry per request: time, duration, member, the request with all inputs (query text and filters included), the full reply or the error, client version, agent name. Network addresses are not logged. Conversations stay on the member's machine.

## 7. Build order

Hub:

1. Stores, settings, admin command, seed import (the first member's existing library, taken in full)
2. Authentication with usage log, API, retrieval, limits; client tools
3. Files, full-text job, passage search
4. Zotero exchange, with the plugin's read functions
5. Backup
6. Intake job: backfill and weekly surveillance

Then workflows (search, discovery, interrogation, with the workflow tool), then collaborative features, then the rest: server-side PDF retrieval (VU-specific, outside the public code) and a web route for members new to agents.

Wish list: cross-encoder reranking of the top retrieval results (Qwen3-Reranker-0.6B), switched by a setting, adopted only if a test shows it helps.

## 8. Open questions

- Intake filter: which model; the department scope statement; whether megajournals and preprint servers pass through it or are limited another way. Settled with harvested data on the server.
- Client settings: how reply size is scoped per agent (tentative: one reply budget with presets, set in `setup` or by a settings tool). Designed during the build.
- Workflows: redesigned for the server and client split after the hub runs. Includes screening at scale, the catch-all for outside search hits, and personal alerts.
- Collaborative features: planned after the workflows.
