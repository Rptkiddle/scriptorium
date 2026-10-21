"""Deterministic library additions: papers by DOI, or by their own metadata.

The single implementation of adding papers to the library ("DOI -> full
Crossref record -> Zotero item", and ready-made item JSON for papers with no
Crossref DOI), shared by every caller (the nightly surveillance add step,
the ingest skill). Callers supply ready-made placement and tags; this
module dedupes against the live library, applies the published-version rule
(see versions), fetches metadata in batched Crossref calls, and creates
items through the bridge with generated citation keys. No LLM involvement.
"""

import re
import time

from scriptorium.apis import CROSSREF, crossref_get, http_session, norm_doi, strip_jats
from scriptorium.config import Config
from scriptorium.versions import (
    VersionIndex,
    is_preprint,
    own_identifiers,
    record_line,
    with_line,
)
from scriptorium.zotero import ZoteroReader, citekeys_of, dois_of
from scriptorium.zotero_write import ZoteroWriter

_HTML = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return _HTML.sub("", strip_jats(text or ""))


# Crossref work type -> Zotero item type (anything else: journalArticle).
_ITEM_TYPES = {
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "posted-content": "preprint",
    "book-chapter": "bookSection",
    "book": "book", "monograph": "book", "edited-book": "book",
}
# Each type's venue field for Crossref's container-title.
_CONTAINER_FIELD = {
    "journalArticle": "publicationTitle", "conferencePaper": "proceedingsTitle",
    "preprint": "repository", "bookSection": "bookTitle",
}
# Fields each type accepts (Zotero 10 schema, /api/itemTypeFields); Zotero
# rejects an item carrying a field its type lacks.
_COMMON = {"title", "abstractNote", "date", "DOI", "url", "citationKey", "extra"}
_ALLOWED = {
    "journalArticle": _COMMON | {"publicationTitle", "volume", "issue", "pages", "ISSN"},
    "conferencePaper": _COMMON | {"proceedingsTitle", "conferenceName", "publisher",
                                  "volume", "issue", "pages", "ISBN", "ISSN"},
    "preprint": _COMMON | {"repository", "archiveID"},
    "bookSection": _COMMON | {"bookTitle", "publisher", "volume", "pages", "ISBN", "ISSN"},
    "book": _COMMON | {"publisher", "volume", "ISBN", "ISSN"},
}


def _fallback_creators(authors) -> list[dict]:
    """The caller's authors (a list of names, or one comma-joined string) as
    Zotero creators, split into first and last name where possible."""
    names = authors if isinstance(authors, list) else (
        [n for n in (authors or "").split(", ") if n] if authors else [])
    creators = []
    for n in names:
        n = str(n).strip()
        if not n or n.lower() in ("et al.", "et al"):
            continue
        first, _, last = n.rpartition(" ")
        creators.append({"creatorType": "author", "firstName": first, "lastName": last}
                        if first else {"creatorType": "author", "name": n})
    return creators


def crossref_record_to_item(record: dict | None, fallback: dict) -> dict:
    """Map a Crossref work record to Zotero item JSON of the matching type
    (journal article, conference paper, preprint, book section, book). Missing
    fields fall back to the caller's own metadata (title, authors as a list
    or comma-joined string, abstract, venue, year). The caller's item_type,
    when given, overrides the type Crossref implies (Crossref files some
    conference papers as journal articles or book chapters), and its
    conference_name fills conferenceName where Crossref has no event. Only
    fields the type accepts are set; a DOI the type cannot hold goes to
    Extra."""
    w = record or {}
    item_type = fallback.get("item_type") or _ITEM_TYPES.get(w.get("type", ""), "journalArticle")
    creators = [
        {"creatorType": "author", "firstName": a.get("given", ""), "lastName": a["family"]}
        for a in w.get("author", []) if a.get("family")
    ] or _fallback_creators(fallback.get("authors"))
    parts = (w.get("issued") or {}).get("date-parts", [[None]])[0]
    date = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts) if p is not None)
    doi = norm_doi(w.get("DOI") or fallback.get("doi", ""))
    container = (w.get("container-title") or [fallback.get("venue", "")])[0]
    publisher = w.get("publisher", "")
    if item_type == "preprint":
        container = ((w.get("institution") or [{}])[0].get("name")
                     or container or publisher)
    fields = {
        "title": _clean((w.get("title") or [fallback.get("title", "")])[0]),
        # Cleaned before the fallback: some publishers deposit an empty
        # "<jats:p/>", which is not an abstract.
        "abstractNote": _clean(w.get("abstract", "")) or _clean(fallback.get("abstract", "")),
        "volume": w.get("volume", ""),
        "issue": w.get("issue", ""),
        "pages": w.get("page", ""),
        "date": date or str(fallback.get("year", "")),
        "DOI": doi,
        "ISSN": ", ".join(w.get("ISSN", [])[:2]),
        "ISBN": ", ".join(w.get("ISBN", [])[:2]),
        "publisher": publisher,
        "conferenceName": ((w.get("event") or {}).get("name", "")
                           or fallback.get("conference_name", "")),
        "url": f"https://doi.org/{doi}",
        "extra": "",
    }
    if item_type in _CONTAINER_FIELD:
        fields[_CONTAINER_FIELD[item_type]] = container
    allowed = _ALLOWED[item_type]
    item = {"itemType": item_type, "creators": creators, "tags": []}
    item |= {k: v for k, v in fields.items() if k in allowed}
    for k in ("volume", "issue", "pages", "ISSN", "ISBN", "publisher", "conferenceName"):
        if k in item and not item[k]:
            del item[k]
    if "DOI" not in allowed and doi:
        item["extra"] = f"DOI: {doi}"
    return item


def _norm_url(url: str) -> str:
    """A URL reduced for comparison: no scheme, "www.", query or trailing slash."""
    url = re.sub(r"^https?://(www\.)?", "", (url or "").strip().lower())
    return url.split("?")[0].split("#")[0].rstrip("/")


def item_from_entry(entry: dict) -> dict:
    """Zotero item JSON from an entry that carries its own metadata
    ({"item": {...}}): a paper with no Crossref DOI (an arXiv preprint, a
    NeurIPS paper, an item assembled by hand). The type must be one of those
    crossref_record_to_item makes, with a title and at least one creator; a
    field the type cannot hold is refused rather than dropped."""
    data = dict(entry["item"])
    item_type = data.pop("itemType", "")
    # Kept: tags join the entry's tags, collections join its collection_key.
    tags, collections = data.pop("tags", None) or [], data.pop("collections", None) or []
    if item_type not in _ALLOWED:
        raise ValueError(f"item type {item_type!r} not supported (use one of"
                         f" {', '.join(sorted(_ALLOWED))})")
    creators = data.pop("creators", None) or []
    unknown = sorted(k for k, v in data.items() if v and k not in _ALLOWED[item_type])
    if unknown:
        raise ValueError(f"a {item_type} has no field {', '.join(unknown)}")
    fields = {k: (norm_doi(v) if k == "DOI" else v) for k, v in data.items() if v}
    if not fields.get("title") or not creators:
        raise ValueError("an item entry needs a title and at least one creator")
    item = {"itemType": item_type, "creators": creators, "tags": tags, **fields}
    if collections:
        item["collections"] = collections
    return item


def entry_id(entry: dict) -> str:
    """The name an entry's outcome is reported under: its DOI; for an item
    entry, its arXiv ID ("arxiv:<id>", also for an arXiv DOI), DOI, URL or
    title."""
    if entry.get("doi"):
        return norm_doi(entry["doi"])
    item = entry.get("item") or {}
    own = own_identifiers(item)
    if arxiv := next((i for i in own if i.startswith("arxiv:")), ""):
        return arxiv
    if doi := next((i for i in own if i.startswith("doi:")), ""):
        return doi.removeprefix("doi:")
    return item.get("url") or f"title: {item.get('title', '')}"


def add_papers(config: Config, entries: list[dict], source: str = "additions",
               dry_run: bool = False) -> dict:
    """Add papers to the library. `source` names the caller (a run) in the
    write log.

    Two kinds of entry, which may be mixed:
    - {"doi", ...fallback metadata (title, authors, abstract, venue, year),
      optionally item_type and conference_name}: the item is built from the
      DOI's Crossref record (see crossref_record_to_item);
    - {"item": {Zotero item JSON}}: a paper with no Crossref DOI, built from
      its own metadata (see item_from_entry).
    Both take "collection_key", "tags": [str] and "extra": str. An entry the
    library already holds is skipped: by an identifier (its DOI or arXiv ID,
    as an item's own or as a preprint recorded in an item's Extra) or, for
    item entries, by URL.

    A published paper is kept as its published version only (see versions):
    - a preprint whose published version (or another preprint of the same
      paper) is in the library, or among this call's entries, is not
      created; its identifier is recorded in that item's Extra;
    - a published paper whose preprint is in the library is created with
      the preprint recorded in its Extra, and the preprint item is moved to
      the trash when it has no child items (attachments, notes) and an
      identifier to record; otherwise it is kept and flagged.

    Outcomes are keyed by entry_id (the DOI, else arXiv ID, URL or title).
    Returns {"created": {id: {"key", "citation_key"}}, "skipped_existing":
    [ids], "versions": {id: {...}}, "failed": {id: reason}, "unverified":
    {id: reason}}. "versions" holds every version match: {"case":
    "recorded" (not created, recorded on matched_key) or "replaces" (created,
    replacing the preprint matched_key), "matched_key", "matched_title",
    "how", and for "replaces" "preprint": "trashed" or "kept: ..."}.
    "unverified" lists items created from the caller's fallback metadata
    because the Crossref lookup failed (created, but worth checking).

    dry_run=True writes nothing: the result lists the entries that would be
    created under "would_create", and each version match says what would
    happen ("preprint": "would be trashed" or "would be kept: ...").
    """
    reader = ZoteroReader(config)
    library = reader.library_snapshot()
    lib_dois, taken = dois_of(library), citekeys_of(library)
    index = VersionIndex(library)
    lib_urls = {u for d in library if (u := _norm_url(d.get("url", "")))}
    result: dict = {"created": {}, "skipped_existing": [], "versions": {},
                    "failed": {}, "unverified": {}}
    if dry_run:
        result["would_create"] = []
    todo: list[tuple[str, dict]] = []  # (entry id, entry)
    items_meta: dict[str, dict] = {}  # entry id -> item JSON
    seen: set[str] = set()
    for e in entries:
        eid = entry_id(e)
        if e.get("doi"):
            e = {**e, "doi": norm_doi(e["doi"])}
            held = e["doi"] in lib_dois or index.holder(e["doi"]) is not None
        else:
            try:
                item = item_from_entry(e)
            except (ValueError, TypeError) as exc:
                result["failed"][eid] = str(exc)
                continue
            held = (any(index.holder(i) is not None for i in own_identifiers(item))
                    or _norm_url(item.get("url", "")) in lib_urls)
            items_meta[eid] = item
        if held or eid in seen:
            result["skipped_existing"].append(eid)
            continue
        seen.add(eid)
        todo.append((eid, e))
    if not todo:
        return result

    dois = [e["doi"] for _, e in todo if e.get("doi")]
    records: dict[str, dict] = {}
    if dois:
        session = http_session("surveillance")
        for i in range(0, len(dois), 40):
            chunk = [d for d in dois[i:i + 40] if "," not in d]
            msg = crossref_get(session, f"{CROSSREF}/works",
                       filter=",".join(f"doi:{d}" for d in chunk), rows=len(chunk))
            for w in msg.get("items", []):
                records[w["DOI"].lower()] = w
            time.sleep(0.2)
        for d in [d for d in dois if d not in records]:
            try:
                records[d] = crossref_get(session, f"{CROSSREF}/works/{d}")
                time.sleep(0.2)
            except Exception as exc:  # noqa: BLE001 — create from fallback metadata
                result["unverified"][d] = f"crossref: {exc}"[:200]

    writer = ZoteroWriter(config, source=source, reader=reader)
    for eid, e in todo:
        item = (crossref_record_to_item(records.get(e["doi"]), e) if e.get("doi")
                else items_meta[eid])
        tags = [*item.get("tags", []), *({"tag": t} for t in e.get("tags", []))]
        item["tags"] = list({t["tag"]: t for t in tags}.values())
        item["extra"] = "\n".join(x for x in (item.get("extra", ""), e.get("extra", "")) if x)
        items_meta[eid] = item

    # Published papers first, so a preprint can be recorded on its published
    # version from the same call. `batch` indexes the items about to be created.
    batch = VersionIndex()
    created_from: dict[int, str] = {}  # id(item) -> entry id, for items in this call
    replaced: dict[str, dict] = {}  # entry id -> the library preprint it replaces
    for eid, e in sorted(todo, key=lambda t: is_preprint(items_meta[t[0]])):
        item, record = items_meta[eid], records.get(e.get("doi", ""))
        match = index.other_version(item, record) or batch.other_version(item, record)
        if match is None:
            batch.add(item)
            created_from[id(item)] = eid
            continue
        other = match.item
        if is_preprint(item):
            # Not created: recorded on the published version (or on the
            # preprint already standing for the paper).
            ident, line = record_line(item)
            entry = {"case": "recorded", "matched_title": other.get("title", ""),
                     "how": match.how}
            if id(other) in created_from:
                other["extra"] = with_line(other.get("extra", ""), ident, line)
                entry["matched_entry"] = created_from[id(other)]
            else:
                entry["matched_key"] = other["key"]
                try:
                    extra = with_line(other.get("extra", ""), ident, line)
                    if extra != (other.get("extra") or "") and not dry_run:
                        writer.update_item(other["key"], {"extra": extra})
                        other["extra"] = extra
                except Exception as exc:  # noqa: BLE001 — record and continue
                    result["failed"][eid] = (f"recording on {other['key']} (another"
                                             f" version) failed: {exc}")
                    continue
            result["versions"][eid] = entry
        else:
            # Created with the library's preprint recorded on it; the
            # preprint is trashed once the create has succeeded.
            ident, line = record_line(other)
            item["extra"] = with_line(item.get("extra", ""), ident, line)
            result["versions"][eid] = {"case": "replaces", "matched_key": other["key"],
                                       "matched_title": other.get("title", ""),
                                       "how": match.how}
            if ident:
                replaced[eid] = other
            else:
                # Nothing to record it by (PhilSci-Archive items carry no DOI
                # or arXiv ID), so trashing it would leave no trace.
                result["versions"][eid]["preprint"] = "kept: no identifier to record"
            batch.add(item)
            created_from[id(item)] = eid

    if dry_run:
        result["would_create"] = [eid for eid, _ in todo
                                  if id(items_meta[eid]) in created_from]
        for eid, preprint in replaced.items():
            children = reader.children(preprint["key"])
            kinds = sorted({c.get("item_type", "") for c in children})
            result["versions"][eid]["preprint"] = (
                f"would be kept: it has {', '.join(kinds)} child items" if children
                else "would be trashed")
        for entry in result["versions"].values():
            if "matched_entry" in entry:
                entry["matched_key"] = f"(created in this run: {entry.pop('matched_entry')})"
        return result

    by_coll: dict[str, list[tuple[str, dict]]] = {}
    for eid, e in todo:
        item = items_meta[eid]
        if id(item) in created_from:
            by_coll.setdefault(e.get("collection_key", ""), []).append((eid, item))
    for coll, pairs in by_coll.items():
        for i in range(0, len(pairs), 50):
            chunk = pairs[i:i + 50]
            try:
                # One library read serves every chunk: create_items adds
                # each key it generates to `taken`.
                r = writer.create_items([it for _, it in chunk], collection_key=coll,
                                        taken=taken)
                # The bridge lists successes in order and keys failures by
                # their index in the chunk; pair keys only with the items
                # that succeeded.
                failed_idx = {int(i): str(reason)
                              for i, reason in (r.get("failed") or {}).items()}
                created_keys = iter(r.get("created", []))
                for idx, (eid, _) in enumerate(chunk):
                    if idx in failed_idx:
                        result["failed"][eid] = failed_idx[idx]
                        continue
                    key = next(created_keys, None)
                    if key is None:
                        result["failed"][eid] = "bridge returned no key"
                        continue
                    result["created"][eid] = {
                        "key": key,
                        "citation_key": (r.get("citation_keys") or {}).get(key, ""),
                    }
            except Exception as exc:  # noqa: BLE001 — record and continue
                for eid, _ in chunk:
                    result["failed"][eid] = str(exc)

    for eid, entry in list(result["versions"].items()):
        if "matched_entry" in entry:
            target_id = entry.pop("matched_entry")
            if target := result["created"].get(target_id):
                entry["matched_key"] = target["key"]
            else:
                del result["versions"][eid]
                result["failed"][eid] = (f"not created as a preprint of {target_id},"
                                         " whose creation failed")
    for eid, preprint in replaced.items():
        entry = result["versions"][eid]
        if eid not in result["created"]:
            entry["preprint"] = "kept: the published version was not created"
            continue
        try:
            if children := reader.children(preprint["key"]):
                kinds = sorted({c.get("item_type", "") for c in children})
                entry["preprint"] = f"kept: it has {', '.join(kinds)} child items"
            else:
                writer.delete_item(preprint["key"])
                entry["preprint"] = "trashed"
        except Exception as exc:  # noqa: BLE001 — the new item stands; report it
            entry["preprint"] = f"kept: {type(exc).__name__}: {exc}"[:200]
    return result
