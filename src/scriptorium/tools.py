"""The hub's MCP tools, served by the daemon (scriptorium.server). Each
tool reaches the daemon's state through daemon.current().

A tool that fails raises, and the caller receives an MCP tool error with
the message. The one exception is hub_attachment_link: once the file is
attached, an indexing failure is reported inside its result, so a caller
never retries and attaches twice."""

import inspect

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from scriptorium import __version__
from scriptorium.daemon import current, log
from scriptorium.search import LIST_LIMIT_MAX, SEARCH_K_MAX
from scriptorium.service import ingest_items
from scriptorium.zotero import ZoteroUnavailableError

mcp = FastMCP("scriptorium")

# How long hub_attachment_link and hub_index wait for a running sync to
# release the index before giving up.
LOCK_WAIT_S = 30


# Claude Code saves a tool reply over 50,000 characters to a file unless the
# tool declares a higher limit; the caps in scriptorium.search keep replies
# near 30,000, and this is the margin for an unusually long page.
REPLY_LIMIT_META = {"anthropic/maxResultSizeChars": 80_000}
# Facets without `contains` on these fields return only the most frequent
# values (the author keys alone run to ~370,000 characters).
FACETS_TOP = 200
_LARGE_FACETS = {"creators", "venues", "venue_folders", "publishers"}

_ITEM_FILTERS = """Filters (all optional; any other key is an error): {"creators":
    [surnames], "creators_all": [surnames], "years": [ints], "year_from":
    int, "year_to": int, "item_types": [...], "categories": [...],
    "publishers": [...], "venue_folders": [...], "added_since":
    "YYYY-MM-DD", "tags": [...], "collections": [collection keys],
    "has_pdf": true/false, "venue": "the exact full venue name (case
    ignored; no partial match)", "citation_keys": [...], "item_keys":
    [...]}. List-valued filters take a list even for one value
    (["Trilling"]). Several values in one filter match ANY of them;
    different filters must ALL hold; "creators_all" requires every listed
    author ("by both X and Y"). Surnames match in any form ("van
    Atteveldt", "Atteveldt", "Wouter van Atteveldt", "Atteveldt, W."):
    case, accents and particles are ignored. Prefer "venue_folders" (the
    library's venue folders) to "venue" (the raw venue field, whose spelling
    varies); resolve folder names with hub_facets(field, contains=...,
    scope="items") first. "Journal articles" can mean item_types
    ["journalArticle"] or categories ["Journals"] (the Journals folder); the
    counts differ. Check the year range with hub_facets("years",
    scope="items") before filtering by year: a filter outside the library's
    stock returns nothing, without error."""


@mcp.tool(meta=REPLY_LIMIT_META)
def hub_search_chunks(query: str, k: int = 10, filters: dict | None = None) -> list[dict]:
    """Search INSIDE the library's papers: dense retrieval over full-text
    chunks of every PDF-indexed paper. Use when the question is about
    content: what the literature says, evidence for a claim, how a method
    works. Returns the top-k most similar chunks, each with citation key,
    page range, similarity score and full chunk text (~5,000 characters
    each, so k up to 7 fits one reply; larger replies may be saved to a
    file instead). To find which items the library holds, use
    hub_search_items. Dense retrieval only; relevance scoring of the
    returned chunks is the caller's job.

    Optional metadata filters restrict the pool BEFORE similarity ranking:
    {"creators": [surnames], "creators_all": [surnames], "years": [ints],
    "year_from": int, "year_to": int, "venue": "the exact full venue name
    (case ignored; no partial match; resolve it with hub_facets('venues'))",
    "citation_keys": [...], "item_keys": [...]}; any other key is an error. Surnames match in
    any form (case, accents and particles ignored); "creators" matches ANY,
    "creators_all" ALL. Resolve venues and years with hub_facets first."""
    d = current()
    try:
        return [r.to_dict() for r in d.search.search(query, k=k, filters=filters)]
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(meta=REPLY_LIMIT_META)
def hub_facets(field: str, contains: str = "", scope: str = "fulltext",
               filters: dict | None = None) -> dict:
    """Distinct metadata values with paper counts, for resolving filter
    values before a filtered search, and for breakdowns. scope "fulltext"
    (default) counts the papers with full text, for hub_search_chunks:
    field is 'creators', 'venues' or 'years'. scope "items" counts every
    library item, for hub_search_items / hub_list_items: the same fields
    plus 'categories', 'publishers', 'venue_folders' and 'item_types'.
    'creators' values are surname keys (lowercase, accents and particles
    removed: "atteveldt" for van Atteveldt), one per surname word.
    `contains`: case-insensitive substring of the value; without it,
    'creators', 'venues', 'venue_folders' and 'publishers' return only the
    200 most frequent values plus "_more": the number of values not shown.
    `filters` counts within a filtered set, e.g. field "years" with
    {"venue_folders": ["New Media & Society"]} gives that venue's items per
    year; scope "items" takes hub_list_items' filters, scope "fulltext" only
    hub_search_chunks' filters."""
    d = current()
    try:
        counts = d.search.facets(field, contains, scope, filters)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if not contains and field in _LARGE_FACETS and len(counts) > FACETS_TOP:
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:FACETS_TOP]
        return {**dict(top), "_more": len(counts) - FACETS_TOP}
    return counts


@mcp.tool()
def hub_search_pool(filters: dict | None = None) -> list[dict]:
    """List the papers hub_search_chunks would draw from under a filter set
    (the retrieval pool): one record per full-text-indexed paper, with
    citation_key and short-form citation. Not a search: use it to inspect
    or sanity-check a pool before searching it (the interrogation skill's
    green-light pass). Same filters schema as hub_search_chunks. Pass
    filters: without them it lists every full-text paper (over 1,000,
    ~300,000 characters), more than an agent's reply holds."""
    d = current()
    try:
        return d.search.papers(filters)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


_SEARCH_ITEMS = inspect.cleandoc(f"""Search FOR library items about a topic: semantic search over the
    catalogue, the WHOLE Zotero library at item level (every item type, with
    or without a PDF). The query is matched against each item's title and
    abstract, so phrase it as a topic (a full title also works). Who, when,
    where and what kind are filters, not query words. Returns up to k (at
    most 40) records, best first: citation, the first 10 authors with given
    names ("Kasper Welbers (ed.)" marks an editor) and author_count, year,
    venue, venue folders, DOI, has_pdf, date_added. abstracts=True adds each
    abstract (up to 2,000 characters; k at most 15), so relevance can be
    judged without opening items. For a complete list or a count of
    everything matching filters (all works by an author, a venue's items for
    a year), use hub_list_items; to check whether a specific paper is in the
    library by DOI, arXiv ID or exact title, use hub_lookup; to read papers'
    content, use hub_search_chunks.

    {_ITEM_FILTERS}""")


@mcp.tool(meta=REPLY_LIMIT_META, description=_SEARCH_ITEMS)
def hub_search_items(query: str, k: int = 20, filters: dict | None = None,
                     abstracts: bool = False) -> list[dict]:
    if not 1 <= k <= SEARCH_K_MAX:
        raise ToolError(f"k must be 1-{SEARCH_K_MAX}; for complete lists or counts of"
                        " items matching filters, use hub_list_items (paged)")
    d = current()
    try:
        return d.search.search_items(query, k=k, filters=filters, abstracts=abstracts)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


_LIST_ITEMS = inspect.cleandoc(f"""List EVERY library item matching filters, without a topic query:
    all works by an author, a venue's papers for a year, the preprints added
    this week. Returns {{"total", "offset", "returned", "next_offset",
    "items"}}: the number of matching items, and one page of short records
    (item key, citation key, citation, the first 10 authors with given
    names and author_count, item type, year, venue folders, DOI, has_pdf,
    date_added). Pages hold up to 60 items; while "next_offset" is not null,
    call again with offset=next_offset. limit=0 returns the total only (a
    count). abstracts=True adds each abstract (up to 2,000 characters;
    limit at most 15). sort: "year" (newest first, default), "added" (most
    recently added first) or "citation" (alphabetical); the order is fixed,
    so pages never overlap. For items ABOUT a topic, use hub_search_items.

    {_ITEM_FILTERS}""")


@mcp.tool(meta=REPLY_LIMIT_META, description=_LIST_ITEMS)
def hub_list_items(filters: dict | None = None, sort: str = "year",
                   limit: int = LIST_LIMIT_MAX, offset: int = 0,
                   abstracts: bool = False) -> dict:
    d = current()
    try:
        return d.search.list_items(filters, sort=sort, limit=limit, offset=offset,
                                   abstracts=abstracts)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def hub_lookup(identifiers: list[str]) -> list[dict]:
    """Is this paper in the library? Exact lookup by DOI, arXiv ID or exact
    title, one or many at once (e.g. a duplicate check before adding
    papers). Accepts bare DOIs, doi.org links, arXiv IDs (2603.13270,
    arXiv:2603.13270v2, arxiv.org links, arXiv DOIs) and titles (matched
    ignoring case, accents and punctuation). Returns one entry per input:
    found or not, and every matching item with how it matched: "doi" or
    "arxiv" (the item's own identifier), "recorded preprint of this item"
    (the input is a preprint whose published version is this item, recorded
    in its Extra field), or "title". An unmatched title also lists the
    closest items by topic search as "candidates", which are not matches.
    For topical searches use hub_search_items."""
    d = current()
    return d.search.lookup(identifiers)


@mcp.tool()
def hub_item(key: str) -> dict:
    """Full record for one library item by Zotero item key or citation key:
    catalogue metadata (title, authors (all, with given names; editors and
    other roles marked), venue, abstract, tags, collections, category,
    publisher and venue folders, date added, DOI, identifiers incl.
    recorded preprints) plus, when the Zotero app is running, live children
    (attachments with resolved local file paths, notes, annotations). To find
    an item by DOI, arXiv ID or title, use hub_lookup."""
    d = current()
    payload = d.hub.catalogue_lookup(key)
    if payload is None:
        raise ToolError(f"no catalogued item with item or citation key {key!r}; to find an"
                        " item by DOI, arXiv ID or title, use hub_lookup")
    record = dict(payload)
    for internal in ("venue_lc", "creator_surnames", "categories_lc", "publishers_lc",
                     "venue_folders_lc", "added_ts", "title_norm"):
        record.pop(internal, None)
    record["chunks_indexed"] = d.hub.state.items.get(record["item_key"], {}).get("chunks", 0)
    try:
        record["children"] = d.reader.children(record["item_key"])
    except ZoteroUnavailableError:
        record["children"] = None
        record["note"] = "Zotero app not running — children/paths unavailable"
    return record


@mcp.tool()
def hub_children(item_key: str) -> list[dict]:
    """List an item's child items (attachments with resolved paths, notes,
    annotations) with their keys — the read side for inspecting or deleting
    notes/annotations. Requires the Zotero app to be running."""
    d = current()
    return d.reader.children(item_key)


@mcp.tool()
def hub_collections(collection_key: str = "") -> list[dict]:
    """Without arguments: the full collection tree (key, name, parent key,
    direct item count). With a collection key: that collection's items.
    Requires the Zotero app to be running."""
    d = current()
    if collection_key:
        return d.reader.collection_items(collection_key)
    return d.reader.collections()


@mcp.tool()
def hub_tags(contains: str = "") -> list:
    """All tags in the library, optionally filtered by substring
    (case-insensitive). Requires the Zotero app to be running."""
    d = current()
    return d.reader.tags(contains)


@mcp.tool()
def hub_item_create(items: list[dict], collection_key: str = "", run: str = "") -> dict:
    """Create Zotero items from COMPLETE Zotero JSON data dicts (itemType,
    title, creators, date, DOI, abstractNote, ...). Metadata must be
    verified by the caller beforehand — this tool performs no external
    lookups. Writes land in the local library instantly via the bridge
    plugin (which requires Zotero running). Returns created item keys and
    their citation keys. `run` (e.g. "ingestion/2026-10-09-1500") names the
    calling run in the write log."""
    d = current()
    return d.writer.create_items(items, collection_key=collection_key, source=run or None)


@mcp.tool()
def hub_item_update(item_key: str, patch: dict, run: str = "") -> dict:
    """Set field values on an existing item, e.g. {"abstractNote": "...",
    "extra": "..."}; fields not in the patch are left as they are. Tags and
    collections (folders) are refused here: change them with hub_item_tags
    and hub_item_collections, which add or remove single entries. Notes are
    child items: add one with hub_note_create, edit one by patching the
    note's own key with {"note": "<html>"}. Applied directly in the local
    library via the bridge plugin. `run` names the calling run in the write
    log."""
    if "itemType" in patch:
        raise ToolError("hub_item_update does not change itemType: use hub_item_type,"
                        " which keeps fields the new type lacks in Extra.")
    lists = sorted({"tags", "collections"} & set(patch))
    if lists:
        raise ToolError(
            f"hub_item_update does not change {' or '.join(lists)}: a patch would"
            " replace the item's whole list. Use hub_item_tags / hub_item_collections,"
            " which add or remove single entries and keep the rest.")
    d = current()
    return d.writer.update_item(item_key, patch, source=run or None)


@mcp.tool()
def hub_item_tags(item_keys: list[str], add: list[str] | None = None,
                  remove: list[str] | None = None, run: str = "") -> dict:
    """Add and/or remove tags on one or more items (item keys, not citation
    keys). Each item's other tags stay as they are; adding a tag an item
    already has, or removing one it lacks, changes nothing. Returns
    {"updated": {item key: its tags afterwards}, "failed": {item key: error}}.
    Existing tags: hub_tags. `run` names the calling run in the write log."""
    d = current()
    return d.writer.edit_tags(item_keys, add or [], remove or [], source=run or None)


@mcp.tool()
def hub_item_collections(item_keys: list[str], add: list[str] | None = None,
                         remove: list[str] | None = None, run: str = "") -> dict:
    """Add one or more items to collections (folders) and/or remove them
    from collections, by collection key (from hub_collections). Each item's
    other collections stay as they are; the collections themselves are not
    changed. An unknown collection key is an error and changes nothing.
    Returns {"updated": {item key: its collection keys afterwards},
    "failed": {item key: error}}. `run` names the calling run in the write
    log."""
    d = current()
    return d.writer.edit_collections(item_keys, add or [], remove or [], source=run or None)


@mcp.tool()
def hub_item_type(item_keys: list[str], item_type: str, dry_run: bool = False,
                  run: str = "") -> dict:
    """Change the item type of one or more items (item keys), e.g. from
    "journalArticle" to "conferencePaper" (Zotero's type names). Zotero moves
    each field to its counterpart in the new type (a journal's
    publicationTitle becomes a paper's proceedingsTitle); fields the new type
    has no place for (e.g. ISSN, issue) are appended to Extra as
    "field: value" lines, so nothing is lost. An author role the new type
    lacks (e.g. a book's "series editor") becomes the new type's main role
    or "contributor". dry_run=True lists the fields that would go to Extra
    without changing anything. Returns {"updated": {item key:
    {"itemType", "movedToExtra": {field: value}}}, "failed": {item key:
    error}, "dryRun"}. `run` names the calling run in the write log."""
    d = current()
    return d.writer.change_type(item_keys, item_type, dry_run=dry_run, source=run or None)


@mcp.tool()
def hub_trash(limit: int = 60, offset: int = 0) -> dict:
    """List what is in Zotero's trash, most recently trashed first: key,
    item type, title (a note's opening text), parent key for child items,
    date modified. Returns
    {"total", "offset", "next_offset", "items"}; while "next_offset" is not
    null, call again with offset=next_offset. Restore items with
    hub_item_restore. Emptying the trash deletes permanently and is left to
    the user in Zotero. Requires the Zotero app to be running."""
    if not 1 <= limit <= LIST_LIMIT_MAX:
        raise ToolError(f"limit must be 1-{LIST_LIMIT_MAX}")
    items = current().reader.trash()
    page = items[offset:offset + limit]
    more = offset + len(page) < len(items)
    return {"total": len(items), "offset": offset,
            "next_offset": offset + len(page) if more else None, "items": page}


@mcp.tool()
def hub_item_restore(item_keys: list[str], run: str = "") -> dict:
    """Take one or more items out of Zotero's trash (keys from hub_trash).
    Returns {"restored": [item keys], "failed": {item key: error}}. `run`
    names the calling run in the write log."""
    d = current()
    return d.writer.restore_items(item_keys, source=run or None)


@mcp.tool()
def hub_note_create(parent_key: str, note: str, run: str = "") -> dict:
    """Add a note to an item (by item key). `note` is plain text (blank
    lines separate paragraphs) or HTML. Returns the new note's key. List an
    item's notes with hub_children; edit one with hub_item_update on the
    note's key ({"note": "<html>"}); remove one with hub_item_delete. `run`
    names the calling run in the write log."""
    d = current()
    return d.writer.create_note(parent_key, note, source=run or None)


@mcp.tool()
def hub_item_delete(item_key: str, run: str = "") -> dict:
    """Move an item to trash — works for any item type, including child
    notes, annotations, and attachments (keys via hub_children). `run` names
    the calling run in the write log."""
    d = current()
    return d.writer.delete_item(item_key, source=run or None)


@mcp.tool()
def hub_collection_create(name: str, parent_key: str = "", run: str = "") -> dict:
    """Create a collection, optionally under a parent collection key. `run`
    names the calling run in the write log."""
    d = current()
    return d.writer.create_collection(name, parent_key=parent_key, source=run or None)


@mcp.tool()
def hub_collection_rename(key: str, name: str, run: str = "") -> dict:
    """Rename a collection. A venue folder's prefix is its subscription
    status ([✓] subscribed, [✗] not), so changing the prefix subscribes or
    unsubscribes the venue at the next surveillance registry rebuild. `run`
    names the calling run in the write log."""
    d = current()
    return d.writer.rename_collection(key, name, source=run or None)


@mcp.tool()
def hub_collection_move(key: str, parent_key: str = "", run: str = "") -> dict:
    """Move a collection under another collection; an empty parent_key
    moves it to the top level. Its key, items and subcollections stay as
    they are. Needs Scriptorium Bridge 0.1.5 or later. `run` names the
    calling run in the write log."""
    d = current()
    return d.writer.move_collection(key, parent_key, source=run or None)


@mcp.tool()
def hub_collection_delete(key: str, force: bool = False, run: str = "") -> dict:
    """Move a collection to Zotero's trash; its items stay in the library.
    Deleting is for mistakes and structure changes: without force=True it
    refuses a collection that holds items or subcollections, and any venue
    folder ([✓] or [✗]), which is kept even when empty as the record of a
    subscription decision. The write log keeps the collection's name,
    parent, subcollections and item keys. `run` names the calling run in
    the write log."""
    d = current()
    return d.writer.delete_collection(key, force=force, source=run or None)


@mcp.tool()
def hub_attachment_link(parent_key: str, pdf_path: str, filename: str = "",
                        run: str = "") -> dict:
    """Attach a local PDF to an existing item as a linked file: the file is
    copied into the linked-attachments folder and the attachment record is
    created directly in the local library via the bridge plugin. Omit
    filename to name the file by the parent's citation key (preferred).
    The paper is full-text indexed in the same call; the result carries
    "indexed". `run` names the calling run in the write log."""
    d = current()
    result = d.writer.link_attachment(parent_key, pdf_path, filename=filename,
                                      source=run or None)
    # The attachment exists now; an indexing failure must not turn that
    # success into a tool error (a caller would retry and attach twice).
    # A running sync holds the write lock: rather than wait for it, leave
    # the paper to the next sync, which sees the new attachment.
    if not d.write_lock.acquire(timeout=LOCK_WAIT_S):
        result["indexed"] = {"deferred": "a sync is running; the next sync indexes this paper"}
        return result
    try:
        result["indexed"] = ingest_items(d.reader, d.hub, d.cfg, [parent_key], log=log)
    except Exception as exc:  # noqa: BLE001 — report, never mask the attach
        result["indexed"] = {"error": f"attached, but indexing failed: {exc}"}
    finally:
        d.write_lock.release()
    return result


@mcp.tool()
def hub_index(item_keys: list[str]) -> dict:
    """(Re)index specific Zotero items into the hub by item key: catalogue
    their metadata and, for items with a PDF, parse, chunk, and embed the
    full text. Requires the Zotero app to be running. Rarely needed
    directly — hub_attachment_link indexes in the same call and the daemon
    auto-syncs when the library changes; use this for targeted re-indexing."""
    d = current()
    if not d.write_lock.acquire(timeout=LOCK_WAIT_S):
        raise ToolError("a sync is running; call hub_index again when it has finished")
    try:
        return ingest_items(d.reader, d.hub, d.cfg, item_keys, log=log)
    finally:
        d.write_lock.release()


@mcp.tool()
def hub_sync() -> dict:
    """Reconcile the hub index with Zotero: ingest new/changed items, remove
    deleted ones. Requires the Zotero app to be running. Interruptible via
    hub_sync_cancel; a subsequent sync resumes where it stopped. Progress is
    visible in /health while running."""
    result = current().run_sync()
    if "error" in result:
        raise ToolError(result["error"])
    return result


@mcp.tool()
def hub_sync_cancel() -> dict:
    """Request cancellation of a running hub_sync. The sync stops cleanly
    after the current paper; already-synced papers keep their state, so the
    next hub_sync resumes where this one stopped."""
    d = current()
    with d.sync_status_lock:
        active = d.sync_status is not None
    if not active:
        return {"cancelled": False, "reason": "no sync running"}
    d.sync_cancel.set()
    return {"cancelled": True}


@mcp.tool()
def hub_status() -> dict:
    """Hub health: index size, sync watermark, failed items, Zotero
    reachability, model info."""
    d = current()
    try:
        d.reader.check_available()
        zotero_running = True
    except ZoteroUnavailableError:
        zotero_running = False
    return {
        "version": __version__,
        **d.hub.counts(),
        "failed_items": d.hub.state.failed,
        "zotero_running": zotero_running,
        "embedding_model": d.cfg.embedding_model,
    }
