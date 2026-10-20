"""Citation key generation, compatible with Better BibTeX's default formula.

Zotero 8+ has a native citationKey field but never populates it; only the
Better BibTeX plugin ever generated keys. This module replicates BBT's
default pattern — auth.lower + shorttitle(3,3) + year, e.g.
``kruseCreatingNextGeneration2023`` — so keys generated here are
stylistically identical to keys the library already carries.

Formula: first creator's family name (particles and hyphens kept, spaces
removed, ASCII-folded, lowercased; institutional creators use the full
name) + the first three title words that are not skip-words (each
capitalised on its first letter only, hyphen parts joined as one word,
ASCII-folded) + the four-digit year (omitted when absent). Collisions
against a caller-supplied set of taken keys get ``a``/``b``/... suffixes.
"""

import re
import unicodedata

# Better BibTeX's skip-word list (articles, prepositions, conjunctions,
# incl. its non-English entries); lowercase comparison.
_SKIP_WORDS = {
    "a", "ab", "aboard", "about", "above", "across", "after", "against",
    "al", "along", "amid", "among", "an", "and", "anti", "around", "as",
    "at", "before", "behind", "below", "beneath", "beside", "besides",
    "between", "beyond", "but", "by", "d", "da", "das", "de", "dei",
    "del", "della", "delle", "dello", "dem", "den", "der", "des",
    "despite", "die", "do", "down", "du", "during", "ein", "eine",
    "einem", "einen", "einer", "eines", "el", "en", "et", "except",
    "for", "from", "gli", "i", "il", "in", "inside", "into", "is", "l",
    "la", "las", "le", "les", "like", "lo", "los", "near", "nor", "of",
    "off", "on", "onto", "or", "over", "past", "per", "plus", "round",
    "save", "since", "so", "some", "sur", "than", "the", "through",
    "to", "toward", "towards", "un", "una", "unas", "und", "under",
    "underneath", "une", "uno", "unos", "unlike", "until", "up", "upon",
    "versus", "via", "von", "while", "with", "within", "without", "y",
    "yet", "zu", "zum",
}

# Characters NFKD decomposition cannot fold to ASCII.
_FOLD = str.maketrans({
    "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D",
    "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D",
})

_TAG_RE = re.compile(r"<[^>]+>")
_YEAR_RE = re.compile(r"\b(\d{4})\b")


def _ascii(text: str) -> str:
    text = text.translate(_FOLD)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _author_part(creators: list[dict]) -> str:
    """First author-role creator: family name lowercased, ASCII, spaces
    removed, hyphens kept; institutional creators contribute the full name."""
    ordered = [c for c in creators if c.get("creatorType") == "author"] or creators
    if not ordered:
        return ""
    first = ordered[0]
    name = first.get("lastName") or first.get("name") or ""
    return re.sub(r"[^a-z0-9-]", "", _ascii(name).lower())


def _title_part(title: str, words: int = 3) -> str:
    """First `words` non-skip-words of the title, first letters capitalised,
    hyphenated words joined as one, joined together."""
    clean = _TAG_RE.sub("", _ascii(title))
    picked: list[str] = []
    for raw in re.split(r"\s+", clean):
        word = re.sub(r"[^A-Za-z0-9]", "", raw)
        if not word or word.lower() in _SKIP_WORDS:
            continue
        picked.append(word[:1].upper() + word[1:])
        if len(picked) == words:
            break
    return "".join(picked)


def generate_citekey(data: dict, taken: set[str] | None = None) -> str:
    """A BBT-default-style citation key for a Zotero item data dict.

    `taken` is the set of keys already in use; collisions get a/b/c suffixes.
    Falls back to the item key when neither creator nor title yields anything.
    """
    year = _YEAR_RE.search(data.get("date", "") or "")
    base = (
        _author_part(data.get("creators", []) or [])
        + _title_part(data.get("title", "") or "")
        + (year.group(1) if year else "")
    )
    if not base:
        base = data.get("key", "item")
    if taken is None:
        return base
    return unique_key(base, taken)


def unique_key(base: str, taken: set[str]) -> str:
    """base if free, else base + a, b, ... z, then aa, ab, ... (BBT style)."""
    if base not in taken:
        return base
    letters = "abcdefghijklmnopqrstuvwxyz"
    for suffix in [*letters, *(a + b for a in letters for b in letters)]:
        if base + suffix not in taken:
            return base + suffix
    raise ValueError(f"no free citation key for {base!r}")
