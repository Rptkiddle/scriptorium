"""Deterministic library additions from Crossref-originated DOIs.

The single implementation of "DOI -> full Crossref record -> Zotero item",
shared by every caller that adds verified candidates to the library (the
nightly surveillance add step, the ingest skill's reference mode). Callers
supply ready-made placement and tags; this module dedupes against the live
library, fetches metadata in batched Crossref calls, and creates items
through the bridge with generated citation keys. No LLM involvement.
"""

import re
import time

from scriptorium.apis import CROSSREF, crossref_get, http_session, norm_doi, strip_jats
from scriptorium.config import Config
from scriptorium.versions import VersionIndex, is_preprint, record_line, with_line
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
    "preprint": _COMMON | {"repository"},
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


def _doi_of(item: dict) -> str:
    """An item's DOI, from its DOI field or (types without one) Extra."""
    if item.get("DOI"):
        return item["DOI"]
    m = re.search(r"DOI:\s*(\S+)", item.get("extra", ""))
    return norm_doi(m.group(1)) if m else ""


def add_by_doi(config: Config, entries: list[dict], source: str = "additions",
               dry_run: bool = False) -> dict:
    """Add papers to the library by DOI. `source` names the caller (a run)
    in the write log.

    Each entry: {"doi", "collection_key", "tags": [str], "extra": str,
    plus fallback metadata (title, authors, abstract, venue, year) and,
    optionally, item_type and conference_name (see crossref_record_to_item)}.
    Entries whose DOI the library already answers to (as an item's own DOI,
    or as a preprint recorded in an item's Extra) are skipped.

    A published paper is kept as its published version only (see versions):
    - a preprint whose published version (or another preprint of the same
      paper) is in the library, or among this call's entries, is not
      created; its identifier is recorded in that item's Extra;
    - a published paper whose preprint is in the library is created with
      the preprint recorded in its Extra, and the preprint item is moved to
      the trash when it has no child items (attachments, notes); otherwise
      it is kept and flagged.

    Returns {"created": {doi: {"key", "citation_key"}}, "skipped_existing":
    [dois], "versions": {doi: {...}}, "failed": {doi: reason}, "unverified":
    {doi: reason}}. "versions" holds every version match: {"case":
    "recorded" (not created, recorded on matched_key) or "replaces" (created,
    replacing the preprint matched_key), "matched_key", "matched_title",
    "how", and for "replaces" "preprint": "trashed" or "kept: ..."}.
    "unverified" lists items created from the caller's fallback metadata
    because the Crossref lookup failed (created, but worth checking).

    dry_run=True writes nothing: the result lists the DOIs that would be
    created under "would_create", and each version match says what would
    happen ("preprint": "would be trashed" or "would be kept: ...").
    """
    reader = ZoteroReader(config)
    library = reader.library_snapshot()
    lib_dois, taken = dois_of(library), citekeys_of(library)
    index = VersionIndex(library)
    todo, skipped = [], []
    seen: set[str] = set()
    for e in entries:
        doi = norm_doi(e["doi"])
        if doi in lib_dois or doi in seen or index.holder(doi) is not None:
            skipped.append(doi)
            continue
        seen.add(doi)
        todo.append({**e, "doi": doi})
    result: dict = {"created": {}, "skipped_existing": skipped, "versions": {},
                    "failed": {}, "unverified": {}}
    if not todo:
        return result

    session = http_session("surveillance")
    records: dict[str, dict] = {}
    dois = [e["doi"] for e in todo]
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
    items_meta: dict[str, dict] = {}
    for e in todo:
        item = crossref_record_to_item(records.get(e["doi"]), e)
        item["tags"] = [{"tag": t} for t in e.get("tags", [])]
        item["extra"] = "\n".join(x for x in (item.get("extra", ""), e.get("extra", "")) if x)
        items_meta[e["doi"]] = item

    # Published papers first, so a preprint can be recorded on its published
    # version from the same call. `batch` indexes the items about to be created.
    batch = VersionIndex()
    created_from: dict[int, str] = {}  # id(item) -> DOI, for items in this call
    replaced: dict[str, dict] = {}  # DOI -> the library preprint it replaces
    for e in sorted(todo, key=lambda e: is_preprint(items_meta[e["doi"]])):
        doi, item = e["doi"], items_meta[e["doi"]]
        match = (index.other_version(item, records.get(doi))
                 or batch.other_version(item, records.get(doi)))
        if match is None:
            batch.add(item)
            created_from[id(item)] = doi
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
                entry["matched_doi"] = created_from[id(other)]
            else:
                entry["matched_key"] = other["key"]
                try:
                    extra = with_line(other.get("extra", ""), ident, line)
                    if extra != (other.get("extra") or "") and not dry_run:
                        writer.update_item(other["key"], {"extra": extra})
                        other["extra"] = extra
                except Exception as exc:  # noqa: BLE001 — record and continue
                    result["failed"][doi] = (f"recording on {other['key']} (another"
                                             f" version) failed: {exc}")
                    continue
            result["versions"][doi] = entry
        else:
            # Created with the library's preprint recorded on it; the
            # preprint is trashed once the create has succeeded.
            ident, line = record_line(other)
            item["extra"] = with_line(item.get("extra", ""), ident, line)
            replaced[doi] = other
            result["versions"][doi] = {"case": "replaces", "matched_key": other["key"],
                                       "matched_title": other.get("title", ""),
                                       "how": match.how}
            batch.add(item)
            created_from[id(item)] = doi

    if dry_run:
        result["would_create"] = [e["doi"] for e in todo
                                  if id(items_meta[e["doi"]]) in created_from]
        for doi, preprint in replaced.items():
            children = reader.children(preprint["key"])
            kinds = sorted({c.get("item_type", "") for c in children})
            result["versions"][doi]["preprint"] = (
                f"would be kept: it has {', '.join(kinds)} child items" if children
                else "would be trashed")
        for entry in result["versions"].values():
            if "matched_doi" in entry:
                entry["matched_key"] = f"(created in this run: {entry.pop('matched_doi')})"
        return result

    by_coll: dict[str, list[dict]] = {}
    for e in todo:
        item = items_meta[e["doi"]]
        if id(item) in created_from:
            by_coll.setdefault(e.get("collection_key", ""), []).append(item)
    for coll, items in by_coll.items():
        for i in range(0, len(items), 50):
            chunk = items[i:i + 50]
            try:
                # One library read serves every chunk: create_items adds
                # each key it generates to `taken`.
                r = writer.create_items(chunk, collection_key=coll, taken=taken)
                # The bridge lists successes in order and keys failures by
                # their index in the chunk; pair keys only with the items
                # that succeeded.
                failed_idx = {int(i): str(reason)
                              for i, reason in (r.get("failed") or {}).items()}
                created_keys = iter(r.get("created", []))
                for idx, item in enumerate(chunk):
                    doi = _doi_of(item)
                    if idx in failed_idx:
                        result["failed"][doi] = failed_idx[idx]
                        continue
                    key = next(created_keys, None)
                    if key is None:
                        result["failed"][doi] = "bridge returned no key"
                        continue
                    result["created"][doi] = {
                        "key": key,
                        "citation_key": (r.get("citation_keys") or {}).get(key, ""),
                    }
            except Exception as exc:  # noqa: BLE001 — record and continue
                for item in chunk:
                    result["failed"][_doi_of(item)] = str(exc)

    for doi, entry in list(result["versions"].items()):
        if "matched_doi" in entry:
            target_doi = entry.pop("matched_doi")
            if target := result["created"].get(target_doi):
                entry["matched_key"] = target["key"]
            else:
                del result["versions"][doi]
                result["failed"][doi] = (f"not created as a preprint of {target_doi},"
                                         " whose creation failed")
    for doi, preprint in replaced.items():
        entry = result["versions"][doi]
        if doi not in result["created"]:
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
