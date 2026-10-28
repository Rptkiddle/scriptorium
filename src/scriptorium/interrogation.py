"""Interrogation: the logic behind the interrogate scripts.

The scripts parse arguments, read and write the run's files, and print;
the rules they apply live here so they can be tested: reading a scoring
judge's reply, splitting chunks across judges, numbering citations, and
rendering BibLaTeX.
"""

import json
import random
import re

# -- copied from PaperQA2 ----------------------------------------------------
# llm_parse_json and strip_citations are copied unchanged (apart from one
# return annotation) from PaperQA2 by FutureHouse, paper-qa 2026.3.18
# (paperqa/core.py, paperqa/utils.py), Apache-2.0: LICENSES/Apache-2.0.txt.


def llm_parse_json(text: str) -> dict:
    """Read LLM output and extract JSON data from it."""
    # Removing <think> tags for reasoning models
    ptext = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # fetches from markdown ```json if present
    ptext = ptext.split("```json")[-1].split("```")[0]

    # Fix specific case with raw fractions in relevance_score
    ptext = re.sub(
        r'"relevance_score"\s*:\s*(\d+)/(\d+)',
        lambda m: f'"relevance_score": {round(int(m.group(1)) / int(m.group(2)) * 10)}',
        ptext,
    )

    # Wrap non-JSON text in a dictionary
    if "{" not in ptext and "}" not in ptext:
        ptext = json.dumps({"summary": ptext})

    # Remove any introductory/closing text and ensure {} to make it a valid JSON
    ptext = ("{" + ptext.split("{", 1)[-1]).rsplit("}", 1)[0] + "}"

    def escape_newlines(match: re.Match) -> str:
        return match.group(0).replace("\n", "\\n")

    # Match anything between double quotes
    # including escaped quotes and other escaped characters.
    # https://regex101.com/r/VFcDmB/1
    ptext = re.sub(r'"(?:[^"\\]|\\.)*"', escape_newlines, ptext)

    # Ensure that any backslashes in the string that are not part
    # of a valid escape sequence are properly escaped
    # https://regex101.com/r/IzMDlI/1
    ptext = re.sub(r'\\([^"\\/bfnrtu])', r"\\\\\1", ptext)

    def fraction_replacer(match: re.Match) -> str:
        key = match.group(1)  # The key (unchanged)

        # Case 1: If quoted fraction `"5/10"`
        if match.group(2) and match.group(3):
            numerator = int(match.group(2))
            denominator = int(match.group(3))

        # Case 2: If unquoted fraction `5/10`
        elif match.group(4) and match.group(5):
            numerator = int(match.group(4))
            denominator = int(match.group(5))

        else:
            return match.group(0)  # No change if no fraction is found

        fraction_value = round(numerator / denominator * 10)  # Convert to integer
        return f"{key}{fraction_value}"

    # Replace X/Y scores with integer value from 0-10
    # e.g. "relevance_score": "8/10" -> "relevance_score": 8
    # e.g. "relevance_score": 3/5 -> "relevance_score": 6
    ptext = re.sub(
        r'("\s*(?:relevance|score)[\w\s\-]*"\s*:\s*)(?:"(\d+)\s*/\s*(\d+)"|(\d+)\s*/\s*(\d+))',
        fraction_replacer,
        ptext,
    )

    # Add missing commas after fields where another key follows
    ptext = re.sub(r'(?<=[}\]0-9"])\s*(?="[^"\\]*"\s*:)', ", ", ptext)

    # Remove extra commas
    ptext = re.sub(r",\s*,+", ",", ptext)  # Remove multiple consecutive commas
    ptext = re.sub(r",\s*}", "}", ptext)  # Remove trailing commas before closing brace
    ptext = re.sub(r"\{\s*,", "{", ptext)  # Remove leading commas inside object

    # Try to parse the JSON normally first
    try:
        data = json.loads(ptext)
    except json.JSONDecodeError as e:
        # If normal parsing fails, try to handle nested quotes case
        if "summary" in ptext and '"relevance_score"' in ptext:
            # Extract summary and relevance_score directly using regex
            summary_match = re.search(
                r'"summary"\s*:\s*"(.*?)",\s*"relevance_score"', ptext, re.DOTALL
            )
            score_match = re.search(r'"relevance_score"\s*:\s*"?(\d+)"?', ptext)
            if summary_match and score_match:
                return {
                    "summary": summary_match.group(1).replace(r"\'", "'"),
                    "relevance_score": int(score_match.group(1)),
                }

        raise ValueError(f"Failed to load JSON from text {text!r}.") from e

    # Handling incorrect key names for "relevance_score"
    for key in list(data):  # List is here to copy keys, since we're in-place mutating
        if re.search(r"relevance|score", key, re.IGNORECASE):
            data["relevance_score"] = data.pop(key)  # Renaming key

    # Handling float, str values for relevance_score
    if "relevance_score" in data and not isinstance(data["relevance_score"], int):
        try:
            data["relevance_score"] = round(float(data["relevance_score"]))
        except ValueError as exc:
            raise ValueError(
                f"Failed to extract 'relevance_score' of {data['relevance_score']!r}"
                " to an integer."
            ) from exc

    return data


def strip_citations(text: str) -> str:
    # Combined regex for identifying citations (see unit tests for examples)
    citation_regex = r"\b[\w\-]+\set\sal\.\s\([0-9]{4}\)|\((?:[^\)]*?[a-zA-Z][^\)]*?[0-9]{4}[^\)]*?)\)"
    # Remove the citations from the text
    return re.sub(citation_regex, "", text, flags=re.MULTILINE)


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
