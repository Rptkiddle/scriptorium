"""Interrogation: the logic behind the interrogate scripts.

The scripts parse arguments, read and write the run's files, and print;
the rules they apply live here so they can be tested: reading a scoring
judge's reply, splitting chunks across judges, numbering citations, and
rendering BibLaTeX.
"""

import random
import re

from paperqa.core import llm_parse_json

# -- relevance scoring --------------------------------------------------------

_SCORE = re.compile(r'"relevance_score"\s*:\s*"?(\d+)"?\s*[,}\n]')


def parse_rcs(raw: str) -> tuple[int, str] | None:
    """A scoring judge's reply as (score, summary), or None when it carries
    no relevance_score on the anchored 0-3 scale. The score is read from the
    raw text first: llm_parse_json rescales fractions ("1/3" -> 3) to
    PaperQA's 0-10."""
    m = _SCORE.search(raw)
    if not m or not 0 <= int(m.group(1)) <= 3:
        return None
    return int(m.group(1)), str(llm_parse_json(raw).get("summary", "") or "")


def split_judges(n_chunks: int, per_judge: int, seed: str) -> list[list[int]]:
    """Chunk indices per scoring judge: shuffled (seeded, so a re-run gives
    the same split) so no judge gets one paper's whole run of chunks; at
    most per_judge each, or one judge for all when per_judge is 0."""
    indices = list(range(n_chunks))
    random.Random(seed).shuffle(indices)
    n = max(1, -(-len(indices) // per_judge)) if per_judge > 0 else 1
    return [sorted(indices[j::n]) for j in range(n)]


# -- numeric citations --------------------------------------------------------

CONTEXT_KEY = re.compile(r"\b([A-Za-z0-9_.\-]+-c\d+)\b")
_PAREN = re.compile(r"\(\s*([A-Za-z0-9_.\-]+-c\d+(?:\s*,\s*[A-Za-z0-9_.\-]+-c\d+)*)\s*\)")


def number_citations(answer: str, sources: dict[str, dict]) -> tuple[str, list[str]]:
    """Replace context-key citations with [n], numbered by first appearance,
    and append a References block. Only keys in `sources` (the surviving
    contexts, by key) are numbered; a parenthetical containing any other key
    is left as written. Returns the final text and the stray keys."""
    numbers: dict[str, int] = {}
    for match in CONTEXT_KEY.finditer(answer):
        key = match.group(1)
        if key in sources and key not in numbers:
            numbers[key] = len(numbers) + 1

    def replace(m: re.Match) -> str:
        keys = [k.strip() for k in m.group(1).split(",")]
        if not all(k in numbers for k in keys):
            return m.group(0)
        return "[" + ", ".join(str(numbers[k]) for k in keys) + "]"

    body = _PAREN.sub(replace, answer)
    refs = ["", "**References**"]
    for key, n in numbers.items():
        c = sources[key]
        pages = c["name"].split(" pages ", 1)[1] if " pages " in c["name"] else "?"
        refs.append(f"[{n}] {key} — {c['citation']} (pages {pages})")
    stray = sorted({m.group(1) for m in CONTEXT_KEY.finditer(body)} - set(numbers))
    return body + "\n" + "\n".join(refs) + "\n", stray


# -- BibLaTeX -----------------------------------------------------------------

BIB_TYPES = {
    "journalArticle": "article",
    "conferencePaper": "inproceedings",
    "book": "book",
    "bookSection": "incollection",
    "preprint": "online",
    "report": "report",
    "thesis": "thesis",
}


def _bib_escape(text: str) -> str:
    return re.sub(r"[{}\\]", "", text or "").replace("&", "\\&").replace("%", "\\%")


def bib_entry(citation_key: str, d: dict) -> str:
    """A BibLaTeX entry for a Zotero item's data dict."""
    kind = BIB_TYPES.get(d.get("itemType", ""), "misc")
    authors = " and ".join(
        f"{c['lastName']}, {c.get('firstName', '')}".rstrip(", ")
        if "lastName" in c else c.get("name", "")
        for c in d.get("creators", []) if c.get("creatorType") in ("author", None)
    )
    year = (re.search(r"\d{4}", d.get("date", "")) or [""])[0] if d.get("date") else ""
    fields = {
        "author": authors,
        "title": _bib_escape(d.get("title", "")),
        "journaltitle": _bib_escape(d.get("publicationTitle", "")),
        "volume": d.get("volume", ""),
        "number": d.get("issue", ""),
        "pages": d.get("pages", ""),
        "date": year,
        "doi": d.get("DOI", ""),
        "abstract": _bib_escape(d.get("abstractNote", "")),
    }
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
    return f"@{kind}{{{citation_key},\n{body}\n}}"
