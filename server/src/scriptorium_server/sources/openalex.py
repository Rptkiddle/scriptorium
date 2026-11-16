"""OpenAlex as a harvest source: a journal's works, page by page, in the item
form the rest of the server uses (Zotero's field names), and the harvest
rules that decide which works become items.

A venue is named by its OpenAlex source ID ("S107737141"); its works are
those whose primary location is that source. The API key travels as the
api_key request parameter, so a request URL is never written to a log or a
run folder: errors are recorded by HTTP status or class name only.

Harvest rules (a work failing one is not admitted; the reason is counted):
  type        research papers only: OpenAlex types "article", "review",
              "conference-paper" (also proceedings in the list, and journals
              OpenAlex mislabels) and "software-paper" (software journals)
  retracted   no retracted papers
  language    English only
A possible book review that OpenAlex types as an article is flagged
(book_review_signs), not dropped, until the signs are tested.
"""

import re
from collections.abc import Callable, Iterator

import requests

from scriptorium_server.identity import norm_doi
from scriptorium_server.sources.abstracts import clean_abstract, openalex_abstract
from scriptorium_server.sources.apis import OPENALEX, request_json

KEPT_TYPES = ("article", "review", "conference-paper", "software-paper")
PER_PAGE = 100
FIELDS = ("id,doi,title,publication_date,publication_year,type,language,is_retracted,"
          "authorships,biblio,primary_location,abstract_inverted_index")
# Lower-case name particles that belong to the family name ("van Houten").
_PARTICLES = {"van", "von", "de", "der", "den", "del", "della", "di", "da", "dos", "das",
              "du", "le", "la", "ten", "ter", "op", "'t", "bin", "al", "el"}
_BOOK_TITLE = re.compile(
    r"\bbook reviews?\b|\breview of\b|\bISBN\b|\bpp\.\s*\d|\$\s?\d|\b\d{3,4}\s*pp\b"
    r"|\b(press|publishers?|publishing)\s*,\s*(19|20)\d\d\b", re.IGNORECASE)


def split_name(name: str) -> dict:
    """A display name as a Zotero creator: the last word (with any particles
    before it) as the family name, the rest as given names; a one-word name
    as a single field."""
    words = (name or "").split()
    if len(words) < 2:
        return {"creatorType": "author", "name": " ".join(words)}
    cut = len(words) - 1
    while cut > 1 and words[cut - 1].lower() in _PARTICLES:
        cut -= 1
    return {"creatorType": "author", "firstName": " ".join(words[:cut]),
            "lastName": " ".join(words[cut:])}


def _pages(biblio: dict) -> str:
    first, last = (biblio or {}).get("first_page"), (biblio or {}).get("last_page")
    if first and last and first != last:
        return f"{first}-{last}"
    return first or ""


def to_record(work: dict) -> dict:
    """A work as a journal-article record."""
    source = ((work.get("primary_location") or {}).get("source") or {})
    doi = norm_doi(work.get("doi") or "")
    # The name as printed on the paper first: it agrees with the publisher's
    # record more often than the author profile's name does.
    creators = [split_name(a.get("raw_author_name")
                           or (a.get("author") or {}).get("display_name") or "")
                for a in work.get("authorships") or []]
    record = {
        "itemType": "journalArticle",
        "title": " ".join((work.get("title") or "").split()),
        "creators": [c for c in creators if c.get("lastName") or c.get("name")],
        "abstractNote": clean_abstract(openalex_abstract(work.get("abstract_inverted_index"))),
        "date": work.get("publication_date") or str(work.get("publication_year") or ""),
        "DOI": doi,
        "publicationTitle": source.get("display_name") or "",
        "volume": (work.get("biblio") or {}).get("volume") or "",
        "issue": (work.get("biblio") or {}).get("issue") or "",
        "pages": _pages(work.get("biblio")),
        "ISSN": ", ".join((source.get("issn") or [])[:2]),
        "url": f"https://doi.org/{doi}" if doi else "",
        "extra": f"OpenAlex: {work.get('id', '').rsplit('/', 1)[-1]}",
    }
    return {k: v for k, v in record.items() if v not in ("", [], None) or k == "creators"}


def rule(work: dict) -> str | None:
    """Why a work is not admitted ("type: editorial", "retracted",
    "language: de"), or None when it passes every rule."""
    if work.get("type") not in KEPT_TYPES:
        return f"type: {work.get('type')}"
    if work.get("is_retracted"):
        return "retracted"
    if (work.get("language") or "") != "en":
        return f"language: {work.get('language')}"
    return None


def book_review_signs(work: dict) -> list[str]:
    """Signs that a work typed as an article is a book review: its title
    names a book's publisher, price, page count or ISBN, or says "book
    review"; or it has no abstract and one or two pages."""
    signs = []
    if _BOOK_TITLE.search(work.get("title") or ""):
        signs.append("title")
    biblio = work.get("biblio") or {}
    try:
        length = int(biblio.get("last_page")) - int(biblio.get("first_page")) + 1
    except (TypeError, ValueError):
        length = None
    if length is not None and 0 < length <= 2 and not work.get("abstract_inverted_index"):
        signs.append("short, no abstract")
    return signs


def works(session: requests.Session, source_id: str, since: str, api_key: str = "",
          on_page: Callable[[dict], None] | None = None) -> Iterator[dict]:
    """Every work whose primary location is the source, published on or
    after `since` (YYYY-MM-DD), of any type, by cursor paging. on_page gets
    each reply's meta (count, cost) as it arrives."""
    params = {"filter": f"primary_location.source.id:{source_id},from_publication_date:{since}",
              "select": FIELDS, "per_page": PER_PAGE, "cursor": "*"}
    if api_key:
        params["api_key"] = api_key
    while params["cursor"]:
        reply = request_json(session, "GET", f"{OPENALEX}/works", params=params)
        if on_page:
            on_page(reply.get("meta") or {})
        yield from reply.get("results") or []
        params["cursor"] = (reply.get("meta") or {}).get("next_cursor")
