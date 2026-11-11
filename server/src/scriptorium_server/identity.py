"""Item identity: which identifiers an item answers to, how author names
are matched, and when two items are versions of one paper.

IDENTIFIERS

An item answers to its own DOI and arXiv ID, and to the preprints recorded
in its Extra field: when a preprint is represented by its published version,
the published item's Extra carries lines such as "arXiv: 2603.13270" or
"SocArXiv: 10.31235/osf.io/abcde". Extra lines are read by their value, not
their label, so any repository name works. Identifiers are stored as
"doi:<doi>" and "arxiv:<id>", lowercased, arXiv versions ("v2") removed.
Titles are matched after normalisation (lowercase, accents, markup and
punctuation removed).

AUTHOR NAME KEYS

One author can be stored several ways in Zotero: "van Atteveldt" or
"Atteveldt" (the particle in the first name), "Damian Trilling" or
"C. Kroon" (the whole name in the surname field), with or without accents.
Every stored author is reduced to keys (lowercase, accents folded, LaTeX
escapes removed), and a filter value is reduced to one key the same way, so
any spelling of a surname finds the author:

- each word of the surname field except particles ("van", "de", ...) and
  initials is a key: "van Atteveldt" -> atteveldt; "García Márquez" ->
  garcia, marquez. A whole name stored in the surname field gives every
  word ("Damian Trilling" -> damian, trilling; "Pham Huong Anh" -> pham,
  huong, anh), since the family name may come first;
- a particle or initial is kept when it is the only word ("Le", "Du");
- a hyphenated surname gives the whole and its parts (typographic hyphens
  and dashes count as "-");
- a filter value is reduced to its surname's last such word ("Wouter van
  Atteveldt", "Atteveldt, W." -> atteveldt).

VERSIONS

A published paper is represented in the collection by its published
version only; the published item records the preprints it stands for as
Extra lines ("SocArXiv: 10.31235/osf.io/abcde", "arXiv: 2603.13270"), which
lookups read. These are pure functions over item data dicts in Zotero's
field names.

Two items are versions of one paper when Crossref links them (a preprint
record's "is-preprint-of", a published record's "has-preprint") or, failing
that, when their normalised titles are equal and they share an author
surname key (any author: the order often changes between a preprint and
its published version). Every pair includes a preprint: two published items
with the same title and authors are separate publications (a conference
paper and its journal extension, for instance).
"""

import html
import re
import unicodedata
from dataclasses import dataclass

# --- identifiers ---------------------------------------------------------------


def norm_doi(doi: str) -> str:
    """A DOI lowercased, without a doi.org URL prefix."""
    doi = (doi or "").strip().lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)


_DOI = re.compile(r"10\.\d{4,9}/\S+")
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$")
_ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[a-z]{2})?/\d{7})(?:v\d+)?")
_ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:\.pdf)?/?$", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def _doi(text: str) -> str:
    """A normalised DOI if `text` is one (bare, doi: or doi.org form), else ""."""
    text = re.sub(r"^doi:\s*", "", norm_doi(text)).rstrip(".,;")
    if not _DOI.fullmatch(text):
        return ""
    # OSF-hosted preprint DOIs (SocArXiv, PsyArXiv, OSF Preprints: 10.3123x)
    # carry a version ("_v1"); the unversioned DOI names the same preprint.
    return re.sub(r"_v\d+$", "", text) if text.startswith("10.3123") else text


def _arxiv(text: str) -> str:
    """An arXiv ID without its version if `text` is one (bare, arXiv: or
    arxiv.org URL form), else ""."""
    text = text.strip()
    if m := _ARXIV_URL.search(text):
        text = m.group(1)
    text = re.sub(r"^arxiv:\s*", "", text, flags=re.IGNORECASE)
    m = _ARXIV_ID.fullmatch(text.lower())
    return m.group(1) if m else ""


def classify(text: str) -> tuple[str, str]:
    """What a lookup input is: ("doi", doi), ("arxiv", id) or ("title",
    normalised title). An arXiv DOI (10.48550/arXiv.<id>) counts as arXiv."""
    if doi := _doi(text):
        m = _ARXIV_DOI.match(doi)
        return ("arxiv", _arxiv(m.group(1)) or m.group(1)) if m else ("doi", doi)
    if arxiv := _arxiv(text):
        return "arxiv", arxiv
    return "title", normalise_title(text)


def _identifier(value: str) -> str:
    """The stored form of a value that is entirely a DOI or arXiv ID, else ""."""
    if not (_doi(value) or _arxiv(value)):
        return ""
    kind, ident = classify(value)
    return f"{kind}:{ident}"


def item_identifiers(doi: str, archive_id: str, extra: str) -> tuple[list[str], list[str]]:
    """(own, recorded): the item's own identifiers (its DOI, its arXiv ID
    from archiveID, a "DOI:" line in Extra for item types without a DOI
    field) and the preprint identifiers recorded in its Extra lines."""
    own, recorded = set(), set()
    for value in (doi, archive_id):
        if value and (ident := _identifier(value)):
            own.add(ident)
    for line in (extra or "").splitlines():
        label, sep, value = line.partition(":")
        if not sep or not (ident := _identifier(value)):
            continue
        (own if label.strip().lower() == "doi" else recorded).add(ident)
    return sorted(own), sorted(recorded - own)


def normalise_title(title: str) -> str:
    """A title reduced for exact comparison: markup and LaTeX escapes
    removed, accents folded, lowercase, punctuation collapsed to spaces."""
    text = _TAG.sub(" ", html.unescape(title or ""))
    text = re.sub(r"\\[a-zA-Z]+|[\\{}$]", " ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


# --- author name keys ----------------------------------------------------------

PARTICLES = {"van", "von", "de", "der", "den", "la", "le", "du", "da", "di", "del", "della",
             "des", "ten", "ter", "dos", "das", "do", "el", "al", "op", "het", "'t", "zu",
             "af", "y", "e", "bin", "binti", "st", "st."}


def _words(text: str) -> list[str]:
    """Lowercased, accent-folded words, LaTeX escapes removed; hyphens and
    inner apostrophes kept."""
    text = re.sub(r"\\[a-zA-Z]+|[\\{}\"]", "", text or "")
    text = re.sub("[\u2010-\u2015\u2212]", "-", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return [w.strip("'.,;:()") for w in re.split(r"[\s,;]+", text) if w.strip("'.,;:()")]


def _is_initial(word: str) -> bool:
    return len(word.rstrip(".")) == 1


def _keep(words: list[str]) -> list[str]:
    """Drop particles and initials, unless nothing would be left."""
    kept = [w for w in words if w not in PARTICLES and not _is_initial(w)]
    return kept or words[-1:]


def _with_parts(words: list[str]) -> set[str]:
    out = set()
    for w in words:
        out.add(w)
        out.update(p for p in w.split("-") if p)
    return out


def creator_keys(creators: list[dict]) -> list[str]:
    """Name keys for Zotero's structured creator list."""
    keys: set[str] = set()
    for c in creators:
        if words := _words(c.get("lastName") or c.get("name") or ""):
            keys |= _with_parts(_keep(words))
    return sorted(keys)


def keys_from_formatted(creators: str) -> list[str]:
    """Name keys from a formatted creators string ("Trilling, Kiddle &
    Welbers"), for records without the structured list."""
    keys: set[str] = set()
    for name in creators.replace(" & ", ", ").split(","):
        if words := _words(name):
            keys |= _with_parts(_keep(words))
    return sorted(keys)


def query_key(value: str) -> str:
    """The key a creators filter value matches: the surname's last word
    that is not a particle or initial. "Surname, Given" forms use the part
    before the comma."""
    if "," in value:
        value = value.split(",", 1)[0]
    words = _words(value)
    return _keep(words)[-1] if words else ""


# Creator roles shown with a mark in author lists; authors, presenters and
# other primary creators are shown unmarked.
_ROLE_MARKS = {"editor": "ed.", "seriesEditor": "series ed.", "translator": "trans.",
               "contributor": "contrib.", "reviewedAuthor": "reviewed author",
               "bookAuthor": "book author", "commenter": "commenter"}


def author_names(creators: list[dict]) -> list[str]:
    """Display names in Zotero's order: "Given Surname", with non-author
    roles marked ("Kasper Welbers (ed.)")."""
    names = []
    for c in creators:
        name = " ".join(p for p in ((c.get("firstName") or "").strip(),
                                    (c.get("lastName") or "").strip()) if p)
        name = name or (c.get("name") or "").strip()
        if not name:
            continue
        mark = _ROLE_MARKS.get(c.get("creatorType", ""))
        names.append(f"{name} ({mark})" if mark else name)
    return names


def authors_from_formatted(creators: str) -> list[str]:
    """Display names from a formatted creators string ("Trilling, Kiddle &
    Welbers"; surnames only), for records without the structured list."""
    return [n.strip() for n in creators.replace(" & ", ", ").split(",") if n.strip()]


# --- versions ------------------------------------------------------------------

PREPRINT = "preprint"


def is_preprint(item: dict) -> bool:
    return item.get("itemType") == PREPRINT


def _stored_identifier(value: str) -> str:
    """The stored form ("doi:...", "arxiv:...") of a DOI or arXiv ID, else ""."""
    kind, ident = classify(value or "")
    return f"{kind}:{ident}" if kind != "title" and ident else ""


def own_identifiers(item: dict) -> list[str]:
    return item_identifiers(item.get("DOI", ""), item.get("archiveID", ""),
                            item.get("extra", ""))[0]


def _author_keys(item: dict) -> set[str]:
    creators = item.get("creators") or []
    authors = [c for c in creators if c.get("creatorType") == "author"] or creators
    return set(creator_keys(authors))


@dataclass
class Match:
    item: dict  # the other version
    how: str  # "crossref is-preprint-of", "crossref has-preprint" or "title + shared author"


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
        ident = _stored_identifier(value)
        return (self.own.get(ident) or self.recorded.get(ident)) if ident else None

    def other_version(self, item: dict, record: dict | None = None) -> Match | None:
        """The indexed item that is another version of `item`, or None.
        `record` is `item`'s Crossref work record; its relation links are
        tried before the title. A published item is preferred over a
        preprint when both match."""
        relation = (record or {}).get("relation") or {}
        name = "is-preprint-of" if is_preprint(item) else "has-preprint"
        for link in relation.get(name, []):
            hit = self.own.get(_stored_identifier(link.get("id", "")))
            if hit is not None and hit is not item and (is_preprint(item) or is_preprint(hit)):
                return Match(hit, f"crossref {name}")
        keys = _author_keys(item)
        title = normalise_title(item.get("title", ""))
        if not keys or not title:
            return None
        hits = [hit for hit in self.titles.get(title, [])
                if hit is not item and (is_preprint(item) or is_preprint(hit))
                and keys & _author_keys(hit)]
        hits.sort(key=is_preprint)
        return Match(hits[0], "title + shared author") if hits else None


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


def rank(preprint: dict) -> int:
    """Which of two preprints of one paper is kept (lowest first): one with a
    preprint server's DOI (SocArXiv, OSF, APSA), then arXiv, then any other
    (PhilSci-Archive)."""
    own = own_identifiers(preprint)
    if any(i.startswith("doi:") for i in own):
        return 0
    return 1 if any(i.startswith("arxiv:") for i in own) else 2


def version_pairs(items: list[dict]) -> list[tuple[dict, dict, str]]:
    """(keep, drop, how) for every pair of versions of one paper among
    `items`: a preprint is dropped for its published version; of two
    preprints, the lower-ranked one (then the later added) is dropped. Each
    item is dropped at most once."""
    index = VersionIndex(items)
    pairs, dropped = [], set()
    for item in items:
        if not is_preprint(item) or item["key"] in dropped:
            continue
        match = index.other_version(item)
        if match is None or match.item["key"] in dropped:
            continue
        other = match.item
        if is_preprint(other):
            keep, drop = sorted([item, other], key=lambda d: (
                rank(d), d.get("dateAdded", ""), d["key"]))
        else:
            keep, drop = other, item
        dropped.add(drop["key"])
        pairs.append((keep, drop, match.how))
    return pairs


def pair_action(keep: dict, drop: dict, children: list[dict]) -> dict:
    """What the library-wide check does with one pair: record the dropped
    preprint's identifier in the kept item's Extra, then trash the dropped
    item unless it has child items (attachments, notes). A dropped item
    with no identifier to record is left alone and flagged. Folders are not
    changed. Returns {"line", "extra" (the kept item's new Extra, or None
    when unchanged), "trash" (bool), "action" (a sentence for the report)}."""
    ident, line = record_line(drop)
    if not ident:
        return {"line": "", "extra": None, "trash": False,
                "action": "flagged: nothing to record it by; left for you"}
    extra = with_line(keep.get("extra", ""), ident, line)
    changed = extra != (keep.get("extra") or "")
    if children:
        kinds = ", ".join(sorted({c.get("item_type", "") for c in children}))
        return {"line": line, "extra": extra if changed else None, "trash": False,
                "action": f"recorded; kept and flagged: it has {kinds} child items"}
    return {"line": line, "extra": extra if changed else None, "trash": True,
            "action": "recorded; trashed"}
