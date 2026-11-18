# scriptorium-bridge

An optional Zotero plugin. With it, the Scriptorium client on the same computer can exchange with your Zotero library:

- **From Scriptorium to Zotero.** Your agent can add items you choose to your library, with their PDFs where the server holds them and hands them out. New items land in a collection named "Scriptorium"; publications your library already holds are skipped.
- **From Zotero to Scriptorium.** You can donate PDFs from your library. Only the PDF and its DOI or arXiv ID leave your computer, and only after you confirm.

The plugin never changes or removes anything in your library. It reads two things: the DOIs and arXiv IDs of your items, and the location of your PDFs. It does not read notes, tags, annotations or collection contents beyond that.

Without the plugin the client still works: items can be exported as BibTeX, PDFs saved to a folder, and PDFs donated from a folder.

## Install

1. Build the plugin file: `cd scriptorium-bridge && zip -j scriptorium-bridge.xpi manifest.json bootstrap.js`
2. In Zotero: Tools → Plugins → gear menu → Install Plugin From File → choose `scriptorium-bridge.xpi`.
3. Ask your agent for Scriptorium's status; it reports whether Zotero and the plugin answer.

It needs Zotero 7 or newer, running while you use it.

## How it works

The plugin adds five functions to the small web server Zotero runs for its own browser connector (port 23119, reachable from your own computer only). Each takes and returns JSON by POST under `/scriptorium/`:

| Function | Takes | Returns |
|---|---|---|
| `ping` | nothing | the plugin's and Zotero's versions |
| `items/identifiers` | nothing | `{identifiers}`: the DOIs and arXiv IDs of the library's items, as `doi:...` and `arxiv:...` |
| `items/create` | `{items, collection?}` | `{created, failed}` by position in `items`; new items go into the named top-level collection, made when missing |
| `attachments/import` | `{parentKey, path}` | `{created}`: the PDF at `path` as a stored attachment of the item |
| `attachments/pdfs` | `{collections?}` | `{pdfs: [{path, identifiers}], without_identifier, missing_file}` for the named collections and their subcollections, or the whole library |

An identifier is read from an item's DOI field, its archive ID, its URL (doi.org and arxiv.org addresses) and the `DOI:` and `arXiv:` lines of its Extra field.
