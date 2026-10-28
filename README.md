<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
  <img src="docs/assets/banner-light.svg" alt="scriptorium" width="100%">
</picture>

Scriptorium is an experimental project in which I explore how AI agents can work with the research literature effectively and semi-autonomously. It centres on a knowledge store (currently: Zotero) and the cycle of work around it. It is motivated by the idea that agentic workflows may be beneficial in interfacing systematically with the sheer number of publications encountered today, especially when working at interdisciplinary junctions.

Currently, a Python daemon indexes the store (SQLite + Qwen3-Embedding-0.6B + GROBID) and serves it as 29 MCP tools for agentic workflows. It reads Zotero via local API and writes through a small Zotero plugin (also included). It also requires access to the Crossref, OpenAlex and (ideally) Semantic Scholar APIs (for now, these are all free). Currently, it also invokes some custom Claude Code 'skills' for certain tasks, but my longer-term aim is to make the package vendor-free.

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

**Hub.** The index holds one catalogue entry per library item (title and abstract embedded; authors, year, item type, venue, folder path, tags and date added stored as filters) and paragraph-level full-text passages for every item with a PDF, parsed by GROBID, with references, captions and tables left out. The daemon re-syncs whenever Zotero's library version changes (on any change to library items, and straight after a write through the hub), and logs every write to Zotero with the item's previous state.

**Skills.**

| Skill | Phase | What it does |
|---|---|---|
| <img src="docs/assets/dog-search.svg" width="140" alt="search"><br>`/scriptorium:search` | 1 | Spot searches and lookups; picks the right search tool and resolves filter values first |
| <img src="docs/assets/dog-ingest.svg" width="140" alt="ingest"><br>`/scriptorium:ingest` | 1 | Adds papers with verified metadata, from PDFs, DOIs or full records, keeping a paper's published version over its preprints |
| <img src="docs/assets/dog-retrieve.svg" width="140" alt="retrieve"><br>`/scriptorium:retrieve` | 1 | Downloads versions of record from publishers' sites, checks each against its title, and attaches on approval |
| <img src="docs/assets/dog-surveil.svg" width="140" alt="surveil"><br>`/scriptorium:surveil` | 2 | Runs one surveillance cycle: Crossref harvest, filter, appraise, digest, add |
| <img src="docs/assets/dog-interrogate.svg" width="140" alt="interrogate"><br>`/scriptorium:interrogate` | 2 | Answers a question from the full text of the library, citing passages with page ranges |
| <img src="docs/assets/dog-discover.svg" width="140" alt="discover"><br>`/scriptorium:discover` | 2 | Protocol-driven search of Semantic Scholar and OpenAlex, with screening and citation snowballing |

## Architecture

```
Claude Code session
  skills ──► scripts/ ──► modules in src/scriptorium/
     │
     │  MCP over HTTP, localhost:8765/mcp
     ▼
daemon (scriptorium serve)
  tools.py    MCP tools
  index.py    SQLite: catalogue + full-text passages
  grobid.py   GROBID, run while PDFs need parsing
  tei.py      its output cut into passages
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
| | `hub_search_chunks` | Passages from papers with an indexed PDF, optionally with their neighbouring paragraphs |
| | `hub_search_pool` | The papers `hub_search_chunks` would draw from under a filter set |
| | `hub_facets` | Filter values with counts |
| | `hub_item` | One item in full, with its PDF path |
| | `hub_children` | An item's attachments and notes |
| | `hub_collections` | The folder tree (or folders by name), or one folder's items |
| | `hub_tags` | Tags in the library |
| | `hub_bibtex` | BibLaTeX or BibTeX entries for citation keys |
| Write | `hub_item_create`, `hub_item_update`, `hub_item_delete` | Create, patch, trash items |
| | `hub_item_tags`, `hub_item_collections` | Add or remove single tags or folders, keeping the rest |
| | `hub_note_create` | Add a note to an item |
| | `hub_item_type` | Change item type; fields the new type lacks go to Extra |
| | `hub_trash`, `hub_item_restore` | List the trash (or search it by title), take items out of it |
| | `hub_collection_create`, `_rename`, `_move`, `_delete` | Folder changes |
| | `hub_attachment_link` | Attach a PDF as a linked file and index it |
| Index | `hub_index`, `hub_sync`, `hub_sync_cancel`, `hub_status` | Index items, reconcile with Zotero, stop a sync, report health |


<p align="center"><img src="docs/assets/divider.svg" width="600" alt=""></p>

## Requirements

- macOS; Apple Silicon recommended (for MPS)
- Zotero 10+, with local API access enabled and the Scriptorium Bridge plugin installed
- (currently) Claude Code, with the Crossref, OpenAlex and Semantic Scholar MCP servers (used by the skills)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Java 21 and [GROBID](https://github.com/grobidOrg/grobid) 0.9.1

## Installation

1. **Zotero.** Enable *Allow other applications on this computer to communicate with Zotero* (Settings › Advanced) and install the Scriptorium Bridge plugin ([`zotero-bridge/README.md`](zotero-bridge/README.md)).
2. **GROBID.** `brew install openjdk@21`. Build GROBID 0.9.1 from its source release (`./gradlew clean assemble`), unpack the two zips it produces (`grobid-service`, `grobid-home`) into `~/.local/share/grobid/install`, run `chmod +x grobid-home/pdfalto/*/pdfalto*`, and add `bindHost: 127.0.0.1` under both connectors in `grobid-home/config/grobid.yaml`.
3. **scriptorium.**

   ```sh
   git clone https://github.com/Rptkiddle/scriptorium.git
   cd scriptorium
   uv sync --extra menubar
   ```

4. **Settings.** Create `data/settings.json`:

   ```json
   {
     "general": {"contact_email": "you@example.org"},
     "zotero": {"data_dir": "~/Zotero", "linked_attachments_dir": "~/Zotero/linked"}
   }
   ```

   `data_dir` is Zotero's data directory; `linked_attachments_dir` is Zotero's Linked Attachment Base Directory (needed to attach PDFs); the contact email goes with Crossref and OpenAlex requests (optional).
5. **First index build** (Zotero open; downloads the embedding model): `SCRIPTORIUM_HF_ONLINE=1 uv run scriptorium sync`
6. **Daemon:** `uv run scriptorium serve`
7. **Claude Code:** `/plugin marketplace add https://github.com/Rptkiddle/scriptorium`, then `/plugin install scriptorium@scriptorium`. Skills other than `search` also use the Crossref, OpenAlex and Semantic Scholar MCP servers.

Optional: `app/build.sh` builds a small app and installs LaunchAgents to run the daemon and menubar icon in the background; open the app once from Finder (Desktop-folder access), then load the agents with the commands it prints.

## Usage

In Claude Code, ask about the library in plain words or invoke a skill:

```
/scriptorium:search  Which papers co-authored by Smith and Jones are in the library?
/scriptorium:interrogate  What does my library say about serendipity in recommender systems?
```

Command line (`sync` refuses while the daemon is running):

```sh
uv run scriptorium status | parse <key> | sync | serve
```

Scripts:

```sh
uv run python scripts/surveil.py [--from YYYY-MM-DD]
uv run python scripts/fetch_pdfs.py --plan | --pilot N | --publisher P | --keys-from <run>/added.json [--retry]
uv run python scripts/attach_pdfs.py --run <fetch run folder> [--review | --accept KEY ... | --reject KEY ...]
uv run python scripts/ingest_add.py --entries <entries.json> [--dry-run]
uv run python scripts/dedupe_versions.py [--apply <run folder>]
```

The daemon serves a dashboard and settings page at `http://localhost:8765/`.

## Configuration

Settings are edited at `/settings` and stored in the data directory (mode 600); API keys are never served back over HTTP. Sections: `general` (sync and idle timers, GROBID, contact email), `zotero` (Zotero's data folder, linked-attachments folder, default BibTeX format), `surveillance` (schedule, research profile, models, abstract-source keys), `interrogation` (retrieval and scoring parameters, models) and `discovery` (Semantic Scholar key, models, caps). Models are aliases (`haiku`, `sonnet`, `opus`, `fable`).

Environment variables read by `config.py`:

| Variable | Default |
|---|---|
| `SCRIPTORIUM_INDEX_DIR` | `data/` in the repository |
| `SCRIPTORIUM_EMBEDDING_MODEL`, `SCRIPTORIUM_EMBEDDING_DIM` | Qwen3-Embedding-0.6B, 256 |

Changing the model requires re-indexing.

## Data layout

Code and data are kept apart: everything scriptorium produces goes to the git-ignored `data` folder in the repository (or wherever a `data` link or `SCRIPTORIUM_INDEX_DIR` points).

```
data/
├── settings.json                 settings
├── index.sqlite                  the index (catalogue, passages, sync state)
├── tei/                          each paper's GROBID output
├── writes.jsonl                  every write to Zotero, with the state before it
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

scriptorium uses PaperQA2 by FutureHouse (Apache-2.0): adapted versions of its evidence-summary and answer prompts, its retrieval defaults, and two helper functions copied into the code (reading a judge's JSON reply, stripping citations). PDF text extraction uses GROBID (Apache-2.0); PyMuPDF (AGPL-3.0) checks fetched PDFs against their titles. The banner lettering is outlined from the UnifrakturMaguntia and EB Garamond fonts (SIL Open Font License 1.1; texts in `LICENSES/`).

Skarlinski, M. D., Cox, S., Laurent, J. M., Braza, J. D., Hinks, M. M., Hammerling, M. J., Ponnapati, M., Rodriques, S. G., & White, A. D. (2024). *Language agents achieve superhuman synthesis of scientific knowledge*. arXiv. https://doi.org/10.48550/arXiv.2409.13740
