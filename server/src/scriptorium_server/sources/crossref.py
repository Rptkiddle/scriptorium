"""Crossref and DataCite as registries: an identifier's record, in the item
form the rest of the server uses (Zotero's field names).

fetch() is what a lookup of an identifier not held calls: a DOI is asked of
Crossref; an arXiv ID of DataCite, where arXiv registers its DOIs. The
record a registry returns is the verification: an identifier that does not
resolve there is not admitted.
"""

import re

import requests

from scriptorium_server.identity import norm_doi
from scriptorium_server.sources.apis import (
    CROSSREF,
    http_session,
    mailto,
    request_json,
    strip_jats,
)

DATACITE = "https://api.datacite.org"


class RegistryUnavailable(RuntimeError):
    """The registry could not be reached or did not answer properly."""


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


def datacite_arxiv_to_item(arxiv_id: str, attributes: dict) -> dict:
    """An arXiv preprint's DataCite record as a preprint item."""
    creators = []
    for c in attributes.get("creators") or []:
        if c.get("familyName"):
            creators.append({"creatorType": "author", "firstName": c.get("givenName", ""),
                             "lastName": c["familyName"]})
        elif c.get("name"):
            creators.append({"creatorType": "author", "name": c["name"]})
    abstract = next((d.get("description", "") for d in attributes.get("descriptions") or []
                     if d.get("descriptionType") == "Abstract"), "")
    year = attributes.get("publicationYear")
    return {
        "itemType": "preprint",
        "title": _clean(((attributes.get("titles") or [{}])[0]).get("title", "")),
        "creators": creators,
        "abstractNote": " ".join(_clean(abstract).split()),
        "date": str(year) if year else "",
        "repository": "arXiv",
        "archiveID": f"arXiv:{arxiv_id}",
        "DOI": f"10.48550/arXiv.{arxiv_id}",
        "url": f"https://arxiv.org/abs/{arxiv_id}",
    }


def fetch(kind: str, value: str, session: requests.Session | None = None
          ) -> tuple[dict, dict | None] | None:
    """(record, relation links) for a DOI ("doi") or arXiv ID ("arxiv"), or
    None when the registry does not know it. RegistryUnavailable when the
    registry cannot be reached."""
    session = session or http_session("lookup")
    try:
        if kind == "arxiv":
            reply = request_json(session, "GET", f"{DATACITE}/dois/10.48550/arxiv.{value}",
                                 not_found={}, attempts=2)
            attributes = (reply.get("data") or {}).get("attributes")
            return (datacite_arxiv_to_item(value, attributes), None) if attributes else None
        reply = request_json(session, "GET", f"{CROSSREF}/works/{norm_doi(value)}",
                             params=mailto(), not_found={}, attempts=2)
    except (requests.RequestException, RuntimeError) as exc:
        raise RegistryUnavailable(f"{type(exc).__name__}: {exc}") from exc
    work = reply.get("message")
    if not work:
        return None
    item = crossref_record_to_item(work, {"doi": value})
    return (item, work.get("relation") or None) if item.get("title") else None

