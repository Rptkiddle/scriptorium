"""Preprint and published versions of one paper.

A published paper is represented in the library by its published version
only; the published item records the preprints it stands for as Extra lines
("SocArXiv: 10.31235/osf.io/abcde", "arXiv: 2603.13270"), which lookups
read. These are pure functions over Zotero item data dicts, shared by every
add path (additions.add_by_doi) and the library-wide check.

Two items are versions of one paper when Crossref links them (a preprint
record's "is-preprint-of", a published record's "has-preprint") or, failing
that, when their normalised titles are equal and their first authors share
a surname key. Every pair includes a preprint: two published items with the
same title and first author are separate publications (a conference paper
and its journal extension, for instance).
"""

from dataclasses import dataclass

from scriptorium.identifiers import classify, item_identifiers, normalise_title
from scriptorium.names import creator_keys

PREPRINT = "preprint"


def is_preprint(item: dict) -> bool:
    return item.get("itemType") == PREPRINT


def _identifier(value: str) -> str:
    """The stored form ("doi:...", "arxiv:...") of a DOI or arXiv ID, else ""."""
    kind, ident = classify(value or "")
    return f"{kind}:{ident}" if kind != "title" and ident else ""


def own_identifiers(item: dict) -> list[str]:
    return item_identifiers(item.get("DOI", ""), item.get("archiveID", ""),
                            item.get("extra", ""))[0]


def _first_author_keys(item: dict) -> set[str]:
    creators = item.get("creators") or []
    authors = [c for c in creators if c.get("creatorType") == "author"] or creators
    return set(creator_keys(authors[:1]))


@dataclass
class Match:
    item: dict  # the other version
    how: str  # "crossref is-preprint-of", "crossref has-preprint" or "title + first author"


class VersionIndex:
    """Items indexed by their own identifiers, the preprint identifiers
    recorded in their Extra, and their normalised titles."""

    def __init__(self, items=()):
        self.own: dict[str, dict] = {}
        self.recorded: dict[str, dict] = {}
        self.titles: dict[str, list[dict]] = {}
        for item in items:
            self.add(item)

    def add(self, item: dict) -> None:
        own, recorded = item_identifiers(item.get("DOI", ""), item.get("archiveID", ""),
                                         item.get("extra", ""))
        for ident in own:
            self.own.setdefault(ident, item)
        for ident in recorded:
            self.recorded.setdefault(ident, item)
        if title := normalise_title(item.get("title", "")):
            self.titles.setdefault(title, []).append(item)

    def holder(self, value: str) -> dict | None:
        """The item that already answers to a DOI or arXiv ID, as its own
        identifier or as a preprint recorded in its Extra."""
        ident = _identifier(value)
        return (self.own.get(ident) or self.recorded.get(ident)) if ident else None

    def other_version(self, item: dict, record: dict | None = None) -> Match | None:
        """The indexed item that is another version of `item`, or None.
        `record` is `item`'s Crossref work record; its relation links are
        tried before the title. A published item is preferred over a
        preprint when both match."""
        relation = (record or {}).get("relation") or {}
        name = "is-preprint-of" if is_preprint(item) else "has-preprint"
        for link in relation.get(name, []):
            hit = self.own.get(_identifier(link.get("id", "")))
            if hit is not None and hit is not item and (is_preprint(item) or is_preprint(hit)):
                return Match(hit, f"crossref {name}")
        keys = _first_author_keys(item)
        title = normalise_title(item.get("title", ""))
        if not keys or not title:
            return None
        hits = [hit for hit in self.titles.get(title, [])
                if hit is not item and (is_preprint(item) or is_preprint(hit))
                and keys & _first_author_keys(hit)]
        hits.sort(key=is_preprint)
        return Match(hits[0], "title + first author") if hits else None


def record_line(preprint: dict) -> tuple[str, str]:
    """(identifier, Extra line) by which a published item records a preprint:
    "arXiv: <id>" for arXiv, else "<repository>: <DOI>". The label is never
    "DOI", which would make the preprint's DOI the item's own. ("", "") when
    the preprint has no identifier."""
    own = own_identifiers(preprint)
    if arxiv := next((i for i in own if i.startswith("arxiv:")), ""):
        return arxiv, f"arXiv: {arxiv.removeprefix('arxiv:')}"
    if doi := next((i for i in own if i.startswith("doi:")), ""):
        label = (preprint.get("repository") or "").strip()
        if not label or label.lower() == "doi":
            label = "Preprint"
        return doi, f"{label}: {doi.removeprefix('doi:')}"
    return "", ""


def with_line(extra: str, ident: str, line: str) -> str:
    """Extra with `line` appended, unless it already holds the identifier."""
    own, recorded = item_identifiers("", "", extra or "")
    if not line or ident in own or ident in recorded:
        return extra or ""
    return f"{extra}\n{line}" if extra else line
