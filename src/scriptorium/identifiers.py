"""Identifiers a library item answers to, for exact lookups (hub_lookup).

An item answers to its own DOI and arXiv ID, and to the preprints recorded
in its Extra field: when a preprint is represented by its published version,
the published item's Extra carries lines such as "arXiv: 2603.13270" or
"SocArXiv: 10.31235/osf.io/abcde". Extra lines are read by their value, not
their label, so any repository name works. Identifiers are stored as
"doi:<doi>" and "arxiv:<id>", lowercased, arXiv versions ("v2") removed.
Titles are matched after normalisation (lowercase, accents, markup and
punctuation removed).
"""

import html
import re
import unicodedata

from scriptorium.apis import norm_doi

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
