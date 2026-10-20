"""Author name keys: what the creators filter and facet match on.

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
- a hyphenated surname gives the whole and its parts;
- a filter value is reduced to its surname's last such word ("Wouter van
  Atteveldt", "Atteveldt, W." -> atteveldt).
"""

import re
import unicodedata

PARTICLES = {"van", "von", "de", "der", "den", "la", "le", "du", "da", "di", "del", "della",
             "des", "ten", "ter", "dos", "das", "do", "el", "al", "op", "het", "'t", "zu",
             "af", "y", "e", "bin", "binti", "st", "st."}


def _words(text: str) -> list[str]:
    """Lowercased, accent-folded words, LaTeX escapes removed; hyphens and
    inner apostrophes kept."""
    text = re.sub(r"\\[a-zA-Z]+|[\\{}\"]", "", text or "")
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
