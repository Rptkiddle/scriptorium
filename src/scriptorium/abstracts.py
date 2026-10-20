"""Abstracts for papers whose Crossref record carries none.

Some publishers deposit no abstracts with Crossref (Elsevier none at all,
some Springer titles neither), so a paper from them would be appraised on
its title alone. fill_abstracts looks the missing ones up by DOI, trying
three sources in turn and sending each source only the DOIs still missing:

  OpenAlex              50 DOIs per request; no key (1,000 requests a day)
  Scopus Search         25 DOIs per request, COMPLETE view; Elsevier API key,
                        and the VU network or an institutional token
  Springer Nature Meta  25 DOIs per request, Springer Nature DOIs only;
                        Springer API key (100 requests a minute, 500 a day)

A source without its key is skipped. A source that refuses access, times out
or stays rate-limited is marked unavailable for the rest of the call, and the
others still run. The recorded reason is the HTTP status or the error's class
name only: request errors quote the URL, and Springer's URL carries the key.
"""

import html
import re

import requests

from scriptorium.apis import OPENALEX, http_session, request_json
from scriptorium.config import CONTACT_EMAIL as MAILTO

SCOPUS_SEARCH = "https://api.elsevier.com/content/search/scopus"
SPRINGER_META = "https://api.springernature.com/meta/v2/json"
SPRINGER_PREFIXES = ("10.1007/", "10.1038/", "10.1057/", "10.1186/")
MAX_CHARS = 2000  # the harvest's cut for Crossref abstracts
SOURCES = ("openalex", "scopus", "springer")


def clean_abstract(text: str) -> str:
    """Plain text: markup and control characters removed, whitespace
    collapsed, a leading "Abstract"/"Summary" label dropped."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^(abstract|summary)\s*[:.]?\s+", "", text, flags=re.IGNORECASE)


def openalex_abstract(inverted: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
    if not inverted:
        return ""
    slots = {pos: word for word, positions in inverted.items() for pos in positions}
    return " ".join(slots[i] for i in sorted(slots))


def scopus_abstract(entry: dict) -> str:
    """dc:description without the publisher's trailing copyright line."""
    text = clean_abstract(entry.get("dc:description") or "")
    return re.sub(r"\s*(©|Copyright ©?)\s*\d{4}.*$", "", text).strip()


def springer_abstract(record: dict) -> str:
    """Springer's abstract is a string, or a dict/list of headed paragraphs;
    the generic "Abstract" heading (h1) is left out."""
    value = record.get("abstract")
    if isinstance(value, str):
        return clean_abstract(value)
    parts: list[str] = []

    def walk(x):
        if isinstance(x, str):
            parts.append(x)
        elif isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            for k, y in x.items():
                if k != "h1":
                    walk(y)

    walk(value)
    return clean_abstract(" ".join(parts))


def _batches(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _reason(exc: Exception) -> str:
    """Why a source stopped, without the exception text (it can hold a key)."""
    response = getattr(exc, "response", None)
    if response is not None:
        return f"HTTP {response.status_code}"
    return type(exc).__name__


def _openalex(session, dois: list[str], settings: dict, found: dict) -> None:
    for batch in _batches([d for d in dois if not re.search(r"[|,]", d)], 50):
        res = request_json(session, "GET", f"{OPENALEX}/works", params={
            "filter": "doi:" + "|".join(f"https://doi.org/{d}" for d in batch),
            "per-page": 50, "select": "doi,abstract_inverted_index", "mailto": MAILTO,
        })
        for work in res.get("results", []):
            doi = (work.get("doi") or "").lower().removeprefix("https://doi.org/")
            text = clean_abstract(openalex_abstract(work.get("abstract_inverted_index")))
            if doi and text:
                found[doi] = text


def _scopus(session, dois: list[str], settings: dict, found: dict) -> None:
    headers = {"X-ELS-APIKey": settings["elsevier_api_key"], "Accept": "application/json"}
    if settings.get("elsevier_insttoken"):
        headers["X-ELS-Insttoken"] = settings["elsevier_insttoken"]
    for batch in _batches([d for d in dois if '"' not in d], 25):
        res = request_json(session, "GET", SCOPUS_SEARCH, headers=headers, attempts=2, params={
            "query": " OR ".join(f'DOI("{d}")' for d in batch),
            "view": "COMPLETE", "count": 25,
        })
        for entry in res.get("search-results", {}).get("entry", []):
            doi = (entry.get("prism:doi") or "").lower()
            text = scopus_abstract(entry)  # an empty result is one entry with "error"
            if doi and text:
                found[doi] = text


def _springer(session, dois: list[str], settings: dict, found: dict) -> None:
    eligible = [d for d in dois
                if d.startswith(SPRINGER_PREFIXES) and not re.search(r'[\s()"]', d)]
    for batch in _batches(eligible, 25):
        res = request_json(session, "GET", SPRINGER_META, attempts=2, params={
            "q": " OR ".join(f"doi:{d}" for d in batch), "p": 25,
            "api_key": settings["springer_api_key"],
        })
        for record in res.get("records", []):
            doi = (record.get("doi") or "").lower()
            text = springer_abstract(record)
            if doi and text:
                found[doi] = text


_LOOKUPS = {"openalex": (_openalex, None),
            "scopus": (_scopus, "elsevier_api_key"),
            "springer": (_springer, "springer_api_key")}


def fill_abstracts(candidates: list[dict], settings: dict,
                   session: requests.Session | None = None) -> dict:
    """Fill the empty "abstract" of each candidate (dicts with "doi" and
    "abstract") in place, cut to MAX_CHARS, and record which source supplied
    it in "abstract_source". `settings` is the surveillance settings section
    (the keys). Returns a summary: how many were missing, how many each source
    filled, how many are left title-only, and which sources were skipped or
    unavailable, with the reason."""
    session = session or http_session("surveillance")
    missing = {c["doi"].lower(): c for c in candidates
               if c.get("doi") and not (c.get("abstract") or "").strip()}
    summary = {"missing": len(missing), **dict.fromkeys(SOURCES, 0), "unavailable": {}}
    for source in SOURCES:
        lookup, key = _LOOKUPS[source]
        if not missing:
            break
        if key and not settings.get(key):
            summary["unavailable"][source] = "no key"
            continue
        found: dict[str, str] = {}  # filled as the lookup goes, so a failure keeps them
        try:
            lookup(session, list(missing), settings, found)
        except (requests.RequestException, RuntimeError) as exc:
            summary["unavailable"][source] = _reason(exc)
        for doi, text in found.items():
            if doi in missing:
                candidate = missing.pop(doi)
                candidate["abstract"] = text[:MAX_CHARS]
                candidate["abstract_source"] = source
                summary[source] += 1
    summary["title_only"] = len(missing)
    return summary


def summary_line(summary: dict) -> str:
    """The report's one-line account of a fill."""
    line = (f"Abstracts filled: openalex {summary['openalex']}, scopus {summary['scopus']},"
            f" springer {summary['springer']} (of {summary['missing']} missing);"
            f" appraised title-only: {summary['title_only']}")
    for source, reason in summary["unavailable"].items():
        line += f"; {source} unavailable ({reason})"
    return line
