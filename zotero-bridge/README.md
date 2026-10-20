# Scriptorium Bridge

A minimal Zotero plugin that registers local HTTP write endpoints on
Zotero's built-in localhost server (port 23119, bound to 127.0.0.1).
Zotero's own local API is read-only; this bridge lets the scriptorium hub
create items, link attachments, and trigger sync directly in the local
library, so writes appear instantly and Zotero's sync uploads them in the
background. The bridge (with Zotero running) is the hub's only write
path — without it, write operations fail with a clear error.

## Install

1. Build the plugin: `cd zotero-bridge && zip -j scriptorium-bridge.xpi manifest.json bootstrap.js`
2. In Zotero: Tools → Plugins → gear menu → Install Plugin From File → select the `.xpi`.
3. Verify: `curl -X POST http://localhost:23119/scriptorium/ping -H "Content-Type: application/json" -d '{}'`

Reinstall the same way after editing the plugin (bump `version` in
`manifest.json` so Zotero picks up the change).

## Endpoints

All accept JSON over POST under `/scriptorium/`:

| Endpoint | Body | Returns |
|---|---|---|
| `ping` | `{}` | `{bridge, zotero}` versions |
| `items/create` | `{items: [web-API item JSON], collectionKey?}` | `{created: [keys], citation_keys, failed}` |
| `items/update` | `{key, patch}` | `{updated, version}` (list fields in the patch replace the item's lists) |
| `items/tags` | `{keys, add?, remove?}` | `{updated: {key: [tags]}, failed}` (other tags kept; 0.1.6+) |
| `items/collections` | `{keys, add?, remove?}` | `{updated: {key: [collection keys]}, failed}` (other collections kept; 0.1.6+) |
| `notes/create` | `{parentKey, note}` | `{created, parentKey}` (child note; 0.1.6+) |
| `items/type` | `{keys, itemType, dryRun?}` | `{updated: {key: {itemType, movedToExtra}}, failed, dryRun}` (fields the new type lacks go to Extra; 0.1.7+) |
| `items/restore` | `{keys}` | `{restored: [keys], failed}` (out of the trash; 0.1.7+) |
| `items/delete` | `{key}` | `{deleted}` (moves to trash) |
| `attachments/link` | `{parentKey, path, title?}` | `{created: [key], file}` |
| `collections/create` | `{name, parentKey?}` | `{created: [key]}` |
| `collections/update` | `{key, name}` | `{updated, name}` (rename) |
| `collections/delete` | `{key}` | `{deleted}` (moves to trash; items stay) |
| `collections/move` | `{key, parentKey}` | `{moved, parentKey}` (empty `parentKey`: top level) |

Item JSON uses the same shape as the Zotero web API (`itemType`, `creators`,
`collections`, `citationKey`, ...). Files linked via `attachments/link`
should already sit under Zotero's Linked Attachment Base Directory so the
stored path is base-relative and portable.
