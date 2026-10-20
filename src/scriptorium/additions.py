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


def add_by_doi(config: Config, entries: list[dict], source: str = "additions") -> dict:
    """Add papers to the library by DOI. `source` names the caller (a run)
    in the write log.

    Each entry: {"doi", "collection_key", "tags": [str], "extra": str,
    plus fallback metadata (title, authors, abstract, venue, year) and,
    optionally, item_type and conference_name (see crossref_record_to_item)}.
    Entries whose DOI is already in the library are skipped. Returns
    {"created": {doi: {"key", "citation_key"}}, "skipped_existing": [dois],
    "failed": {doi: reason}, "unverified": {doi: reason}} — "unverified"
    lists items created from the caller's fallback metadata because the
    Crossref lookup failed (created, but worth checking).
    """
    reader = ZoteroReader(config)
    library = reader.library_snapshot()
    lib_dois, taken = dois_of(library), citekeys_of(library)
    todo, skipped = [], []
    seen: set[str] = set()
    for e in entries:
        doi = norm_doi(e["doi"])
        if doi in lib_dois or doi in seen:
            skipped.append(doi)
            continue
        seen.add(doi)
        todo.append({**e, "doi": doi})
    result: dict = {"created": {}, "skipped_existing": skipped, "failed": {},
                    "unverified": {}}
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
    by_coll: dict[str, list[dict]] = {}
    items_meta: dict[str, dict] = {}
    for e in todo:
        item = crossref_record_to_item(records.get(e["doi"]), e)
        item["tags"] = [{"tag": t} for t in e.get("tags", [])]
        item["extra"] = "\n".join(x for x in (item.get("extra", ""), e.get("extra", "")) if x)
        by_coll.setdefault(e.get("collection_key", ""), []).append(item)
        items_meta[e["doi"]] = item
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
    return result
