<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
  <img src="docs/assets/banner-light.svg" alt="scriptorium" width="100%">
</picture>

Scriptorium is an experimental project in which I explore how AI agents can work with the research literature effectively and semi-autonomously. It centres on a knowledge store (currently: Zotero) and the cycle of work around it. It is motivated by the idea that agentic workflows may be beneficial in interfacing systematically with the sheer number of publications encountered today, especially when working at interdisciplinary junctions.

Currently, a Python daemon indexes the store (embedded Qdrant + Qwen3-Embedding-0.6B + PaperQA2's modified PyMuPDF reader) and serves it as 22+ MCP tools for agentic workflows. It reads Zotero via local API and writes through a small Zotero plugin (also included). It also requires access to the Crossref, OpenAlex and (ideally) Semantic Scholar APIs (for now, these are all free). Currently, it also invokes some custom Claude Code 'skills' for certain tasks, but my longer-term aim is to make the package vendor-free.

I am a PhD candidate and postdoctoral researcher in Communication Science at Vrije Universiteit Amsterdam. I built scriptorium as a working tool for my own research, and as an experimental project in learning to interface with a voluminous interdisciplinary literature. I share it as a tool under heavy development, currently quite personalised to my own workflow, and liable to change and break. If you have questions, or want to collaborate, feel free to reach me at r.t.kiddle@vu.nl.

<p align="center"><img src="docs/assets/divider.svg" width="600" alt=""></p>

## Contents

- [Roadmap](#roadmap)
- [Features](#features)
- [Architecture](#architecture)
- [MCP tools](#mcp-tools)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Data layout](#data-layout)
- [Development](#development)
- [Licence](#licence)
- [Citation and acknowledgements](#citation-and-acknowledgements)

## Roadmap

**Phase 1 – the library interface.** Phase 1 provides an interface to Zotero for agents. Through it, agents can efficiently and reliably look up a paper by DOI, arXiv ID or title, list and count items by author, venue, year or folder, search by topic, read passages from full texts, and change items and folders. Three skills build on this interface: `search` for semantic spot searches and lookups, `ingest` for adding papers with Crossref-verified metadata, and `retrieve` for obtaining PDFs. 

**Phase 2 – surveillance, interrogation and discovery.** Phase 2 provides more advanced agentic workflows: surveillance (a nightly filter-then-appraise check of new publications in subscribed venues), interrogation (adaptation of FutureHouse's PaperQA2 approach to asking questions of a large-N library) and discovery (systematic literature search, still under development). 

**Phase 3 – a concept-level self-maintaining knowledge base.** The longer-term aim is a semi-autonomous (human-in-the-loop) knowledge base that maintains itself alongside the Zotero library and works at the level of concepts; providing a 'map' of relevant interdisciplinary literature as regards each concept to be utilised by the surveillance, interrogation, and discovery skills.   

## Features

**Hub.** The index holds one catalogue entry per library item (title and abstract embedded; authors, year, item type, venue, folder path, tags and date added stored as filters) and overlapping full-text passages of about 5,000 characters for every item with a PDF, with the reference list removed. The daemon re-syncs whenever Zotero's library version changes (on any change to library items), and logs every write to Zotero with the item's previous state.

**Skills.**

| | Skill | Phase | What it does |
|---|---|---|---|
| <img src="docs/assets/dog-search.svg" width="140" alt="search"> | `/scriptorium:search` | 1 | Spot searches and lookups; picks the right search tool and resolves filter values first |
| <img src="docs/assets/dog-ingest.svg" width="140" alt="ingest"> | `/scriptorium:ingest` | 1 | Adds papers with verified metadata, from PDFs in a local path or from DOIs |
| <img src="docs/assets/dog-retrieve.svg" width="140" alt="retrieve"> | `/scriptorium:retrieve` | 1 | Downloads versions of record from publishers' sites, checks each against its title, and attaches on approval |
| <img src="docs/assets/dog-surveil.svg" width="140" alt="surveil"> | `/scriptorium:surveil` | 2 | Runs one surveillance cycle: Crossref harvest, filter, appraise, digest, add |
| <img src="docs/assets/dog-interrogate.svg" width="140" alt="interrogate"> | `/scriptorium:interrogate` | 2 | Answers a question from the full text of the library, citing passages with page ranges |
| <img src="docs/assets/dog-discover.svg" width="140" alt="discover"> | `/scriptorium:discover` | 2 | Protocol-driven search of Semantic Scholar and OpenAlex, with screening and citation snowballing |

## Architecture

```
Claude Code session
  skills ──► scripts/ ──► modules in src/scriptorium/
     │
     │  MCP over HTTP, localhost:8765/mcp
     ▼
daemon (scriptorium serve)
  tools.py    MCP tools
  index.py    embedded Qdrant: catalogue + full-text chunks
  search.py   filters, lookup, listing
  routes.py   dashboard and settings pages
     │                              │
     │ read: Zotero local API       │ write: Scriptorium Bridge (localhost:23119)
     ▼                              ▼
Zotero (PDFs as linked files, named by citation key)

launchd ──► launcher.py ──► surveillance_cycle.py ──► claude -p (judging only, no tools)
```


## MCP tools

| Group | Tool | Purpose |
|---|---|---|
| Read | `hub_lookup` | Exact match on DOI, arXiv ID or title, many at once |
| | `hub_list_items` | Every item matching filters, paged, with a total; `limit=0` counts |
| | `hub_search_items` | Items about a topic (title and abstract), narrowed by filters |
| | `hub_search_chunks` | Passages from papers with an indexed PDF |
| | `hub_search_pool` | The papers `hub_search_chunks` would draw from under a filter set |
| | `hub_facets` | Filter values with counts |
| | `hub_item` | One item in full, with its PDF path |
| | `hub_children` | An item's attachments and notes |
| | `hub_collections` | The folder tree, or one folder's items |
| | `hub_tags` | Tags in the library |
| Write | `hub_item_create`, `hub_item_update`, `hub_item_delete` | Create, patch, trash items |
| | `hub_item_tags`, `hub_item_collections` | Add or remove single tags or folders, keeping the rest |
| | `hub_note_create` | Add a note to an item |
| | `hub_collection_create`, `_rename`, `_move`, `_delete` | Folder changes |
| | `hub_attachment_link` | Attach a PDF as a linked file and index it |
| Index | `hub_index`, `hub_sync`, `hub_sync_cancel`, `hub_status` | Index items, reconcile with Zotero, stop a sync, report health |


<p align="center"><img src="docs/assets/divider.svg" width="600" alt=""></p>

## Requirements

- macOS; Apple Silicon recommended (for MPS)
- Zotero 10+, with local API access enabled and the Scriptorium Bridge plugin installed
- (currently) Claude Code, with the Crossref, OpenAlex and Semantic Scholar MCP servers (used by the skills)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)

## Installation

```sh
git clone https://github.com/Rptkiddle/scriptorium.git
cd scriptorium
uv sync --extra menubar
SCRIPTORIUM_HF_ONLINE=1 uv run scriptorium sync   # first index build; downloads the embedding model
```

1. In Zotero, enable *Allow other applications on this computer to communicate with Zotero* (Settings › Advanced), install the bridge ([`zotero-bridge/README.md`](zotero-bridge/README.md)), and set the Linked Attachment Base Directory to `zotero.linked_attachments_dir`.
2. Build the app bundle with `app/build.sh` and open `~/Applications/Scriptorium.app` once from Finder to grant Desktop access (again after every rebuild).
3. Copy the two plists in `launchd/` to `~/Library/LaunchAgents`, adjust their paths, and load each with `launchctl bootstrap gui/$UID <plist>`.
4. In Claude Code: `/plugin marketplace add <path or URL of this repo>`, then `/plugin install scriptorium@scriptorium`.

## Usage

In Claude Code, ask about the library in plain words or invoke a skill:

```
/scriptorium:search  Which papers co-authored by Smith and Jones are in the library?
/scriptorium:interrogate  What does my library say about serendipity in recommender systems?
```

Command line (`search` and `sync` refuse while the daemon is running):

```sh
uv run scriptorium status | items | show <key> | parse <key> | search "<query>" | sync | serve
```

Scripts:

```sh
uv run python scripts/surveil.py [--from YYYY-MM-DD]
uv run python scripts/fetch_pdfs.py --plan | --pilot N | --publisher P
uv run python scripts/attach_pdfs.py --run <fetch run folder>
uv run python scripts/ingest_add.py --entries <entries.json>
```

The daemon serves a dashboard and settings page at `http://localhost:8765/`.

## Configuration

Settings are edited at `/settings` and stored in the data directory (mode 600); API keys are never served back over HTTP. Sections: `general` (sync and idle timers), `zotero` (linked-attachments folder), `surveillance` (schedule, research profile, models, abstract-source keys), `interrogation` (retrieval and scoring parameters, models) and `discovery` (Semantic Scholar key, models, caps). Models are aliases (`haiku`, `sonnet`, `opus`, `fable`).

Environment variables read by `config.py`:

| Variable | Default |
|---|---|
| `SCRIPTORIUM_INDEX_DIR` | the data directory |
| `SCRIPTORIUM_ZOTERO_DATA` | Zotero's data directory |
| `SCRIPTORIUM_EMBEDDING_MODEL`, `SCRIPTORIUM_EMBEDDING_DIM` | Qwen3-Embedding-0.6B, 256 |
| `SCRIPTORIUM_CHUNK_CHARS`, `SCRIPTORIUM_CHUNK_OVERLAP` | 5000, 250 |

Changing the chunk sizes or the model requires re-indexing.

## Data layout

Code and data are kept apart; the repository's git-ignored `data` link points to the data directory.

```
data/
├── settings.json, state.json     settings; index sync state
├── writes.jsonl                  every write to Zotero, with the state before it
├── qdrant/                       the index
└── <activity>/runs/<YYYY-MM-DD-HHMMSS>[-<topic>]/
    ├── run.json                  status, summary, commit, history
    ├── report.md
    └── work/                     prompts, replies, verdicts, intermediate files
```

Logs are in `~/Library/Logs/scriptorium/`.

## Development

```sh
uv run pytest    # no network, Zotero or Claude needed
```

Before pushing, `scripts/check_push.sh` scans the outgoing commits for secrets and personal details; install it as the pre-push hook with `ln -s ../../scripts/check_push.sh .git/hooks/pre-push`.

<p align="center"><img src="docs/assets/divider.svg" width="600" alt=""></p>

## Licence

Not yet licensed. I intend to release scriptorium under the GNU Affero General Public License 3.0, with a commercial licence available on request. Prompt text adapted from PaperQA2 falls under Apache-2.0 (`LICENSES/`).

## Citation and acknowledgements

To cite scriptorium, see [`CITATION.cff`](CITATION.cff).

scriptorium uses PaperQA2 by FutureHouse (Apache-2.0): its document reader, chunking and retrieval defaults, and adapted versions of its evidence-summary and answer prompts. PDF text extraction uses PyMuPDF (AGPL-3.0). The banner lettering is outlined from the UnifrakturMaguntia and EB Garamond fonts (SIL Open Font License 1.1; texts in `LICENSES/`).

Skarlinski, M. D., Cox, S., Laurent, J. M., Braza, J. D., Hinks, M. M., Hammerling, M. J., Ponnapati, M., Rodriques, S. G., & White, A. D. (2024). *Language agents achieve superhuman synthesis of scientific knowledge*. arXiv. https://doi.org/10.48550/arXiv.2409.13740
