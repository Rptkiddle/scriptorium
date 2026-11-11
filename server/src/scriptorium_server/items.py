"""Items: admitting a publication to the collection under the identity rules.

A record is a dict in Zotero's field names (itemType, title, creators,
date, DOI, archiveID, extra, abstractNote, publicationTitle, ...), the one
form every source, donation and lookup produces. admit() is the only way
an item enters the collection:

- a record whose DOI or arXiv ID an item already answers to (as its own
  identifier or as a recorded preprint) is that item;
- a preprint of a held paper is recorded on the held item, not added;
- a published version arriving after its preprint replaces the preprint
  in place (same key) and records the preprint's identifier;
- anything else becomes a new item.

Two sessions admitting the same publication at once give one item: admit()
takes a lock on each of the record's identifiers and on its title for the
length of its transaction, so the second session waits and then finds the
first one's item.
"""

import hashlib
import re
import secrets

import psycopg
from psycopg.types.json import Jsonb

from scriptorium_server.citekeys import generate_citekey, unique_key
from scriptorium_server.identity import (
    VersionIndex,
    creator_keys,
    is_preprint,
    item_identifiers,
    normalise_title,
    own_identifiers,
    record_line,
    with_line,
)

PROVENANCE = ("venue", "donation", "activity")
_KEY_ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"  # Zotero's: no 0, 1, O
_VENUE_FIELDS = ("publicationTitle", "proceedingsTitle", "repository", "bookTitle")
_CATEGORIES = {"journalArticle": "Journals", "conferencePaper": "Proceedings",
               "preprint": "Preprints", "book": "Books", "bookSection": "Books"}
# Record fields held in columns of their own; the rest go to `data`.
_COLUMN_FIELDS = {"key", "citationKey", "itemType", "title", "abstractNote", "date", "DOI",
                  "creators", "publisher"}
_YEAR = re.compile(r"\b(\d{4})\b")


def to_record(row: dict) -> dict:
    """An items row as a record in Zotero's field names."""
    record = dict(row["data"])
    record.update(key=row["key"], itemType=row["type"], title=row["title"],
                  creators=row["creators"], DOI=row["doi"] or "",
                  abstractNote=row["abstract"] or "", date=row["date"] or "",
                  citationKey=row["citation_key"] or "")
    if row["arxiv_id"] and not record.get("archiveID"):
        record["archiveID"] = f"arXiv:{row['arxiv_id']}"
    return record


def _lock(conn: psycopg.Connection, names: list[str]) -> None:
    """Hold a lock on each name until the transaction ends. Always taken
    in the same order, so two sessions cannot wait on each other."""
    numbers = sorted({int.from_bytes(hashlib.sha256(n.encode()).digest()[:8], "big", signed=True)
                      for n in names})
    for number in numbers:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (number,))


def holder(conn: psycopg.Connection, identifiers: list[str]) -> str | None:
    """The key of the item that answers to one of the identifiers
    ("doi:...", "arxiv:..."), as its own or as a recorded preprint."""
    for ident in identifiers:
        kind, _, value = ident.partition(":")
        column = {"doi": "doi", "arxiv": "arxiv_id"}[kind]
        row = conn.execute(
            f"SELECT key FROM items WHERE lower({column}) = %s OR other_ids @> %s LIMIT 1",
            (value.lower(), Jsonb([ident]))).fetchone()
        if row:
            return row["key"]
    return None


def _columns(record: dict) -> dict:
    """The identity and bibliographic columns a record fills."""
    own, recorded = item_identifiers(record.get("DOI", ""), record.get("archiveID", ""),
                                     record.get("extra", ""))
    dois = [i.removeprefix("doi:") for i in own if i.startswith("doi:")]
    arxivs = [i.removeprefix("arxiv:") for i in own if i.startswith("arxiv:")]
    spare = [f"doi:{d}" for d in dois[1:]] + [f"arxiv:{a}" for a in arxivs[1:]]
    year = _YEAR.search(record.get("date", "") or "")
    return {
        "type": record["itemType"],
        "title": record["title"].strip(),
        "title_norm": normalise_title(record["title"]),
        "abstract": (record.get("abstractNote") or "").strip() or None,
        "year": int(year.group(1)) if year else None,
        "date": record.get("date") or None,
        "doi": dois[0] if dois else None,
        "arxiv_id": arxivs[0] if arxivs else None,
        "other_ids": Jsonb(sorted({*recorded, *spare})),
        "creators": Jsonb(record.get("creators") or []),
        "data": Jsonb({k: v for k, v in record.items()
                       if k not in _COLUMN_FIELDS and v not in ("", None, [])}),
    }


def _venue(conn: psycopg.Connection, record: dict, publisher: str | None,
           category: str | None, venue: str | None = None) -> dict | None:
    """The venue row for the venue name (the caller's, else the record's),
    created if new."""
    name = (venue or "").strip() or next(
        (record[f].strip() for f in _VENUE_FIELDS if (record.get(f) or "").strip()), "")
    if not name:
        return None
    return conn.execute(
        "INSERT INTO venues (name, publisher, category) VALUES (%s, %s, %s)"
        " ON CONFLICT (lower(name)) DO UPDATE SET name = venues.name"
        " RETURNING id, publisher, category", (name, publisher, category)).fetchone()


def _placement(conn, record, publisher, category, venue_name=None) -> dict:
    venue = _venue(conn, record, publisher or record.get("publisher"), category, venue_name)
    return {
        "venue_id": venue["id"] if venue else None,
        "publisher": (publisher or record.get("publisher")
                      or (venue["publisher"] if venue else None)),
        "category": (category or (venue["category"] if venue else None)
                     or _CATEGORIES.get(record["itemType"])),
    }


def _write_creators(conn: psycopg.Connection, key: str, creators: list[dict]) -> None:
    conn.execute("DELETE FROM item_creators WHERE item = %s", (key,))
    rows = [(key, name_key, position) for position, creator in enumerate(creators or [])
            for name_key in creator_keys([creator])]
    if rows:
        conn.cursor().executemany(
            "INSERT INTO item_creators (item, name_key, position) VALUES (%s, %s, %s)"
            " ON CONFLICT DO NOTHING", rows)


def _new_key(conn: psycopg.Connection) -> str:
    while True:
        key = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(8))
        if not conn.execute("SELECT 1 FROM items WHERE key = %s", (key,)).fetchone():
            return key


def _citation_key(conn: psycopg.Connection, record: dict) -> str:
    """The record's own citation key, else one generated from author, title
    and year; given a suffix when another item has it already."""
    base = (record.get("citationKey") or "").strip() or generate_citekey(record)
    pattern = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute("SELECT citation_key FROM items WHERE citation_key LIKE %s", (pattern,))
    return unique_key(base, {row["citation_key"] for row in rows})


def insert(conn, record, provenance_kind, member=None, publisher=None, category=None,
           venue=None) -> str:
    """Create an item from a record without looking for other versions of
    it; returns its key. For callers whose records are already one per
    publication (the seed import); everything else goes through admit()."""
    key = (record.get("key") or "").strip() or _new_key(conn)
    values = {"key": key, "citation_key": _citation_key(conn, record),
              "provenance_kind": provenance_kind, "provenance_member": member,
              **_columns(record), **_placement(conn, record, publisher, category, venue)}
    names = ", ".join(values)
    marks = ", ".join(f"%({name})s" for name in values)
    conn.execute(f"INSERT INTO items ({names}) VALUES ({marks})", values)
    _write_creators(conn, key, record.get("creators") or [])
    return key


def _record_preprint(conn: psycopg.Connection, held: dict, ident: str, line: str) -> None:
    """Record a preprint's identifier on the held item."""
    extra = with_line(held.get("extra", ""), ident, line)
    conn.execute(
        "UPDATE items SET data = data || %s,"
        " other_ids = (SELECT jsonb_agg(DISTINCT x) FROM jsonb_array_elements("
        "              other_ids || %s) AS x) WHERE key = %s",
        (Jsonb({"extra": extra}), Jsonb([ident]), held["key"]))


def _replace(conn, preprint: dict, record: dict, publisher, category) -> None:
    """Put the published record in the preprint's place: same key, citation
    key, provenance and date added; the preprint's identifier, and any
    preprints recorded on it, are recorded on the published item. The
    vector is emptied so the item is embedded again from its new text."""
    extra = record.get("extra", "") or ""
    ident, line = record_line(preprint)
    extra = with_line(extra, ident, line)
    for old_line in (preprint.get("extra") or "").splitlines():
        for old_ident in item_identifiers("", "", old_line)[1]:
            extra = with_line(extra, old_ident, old_line)
    values = {**_columns({**record, "extra": extra}),
              **_placement(conn, record, publisher, category), "key": preprint["key"]}
    sets = ", ".join(f"{name} = %({name})s" for name in values if name != "key")
    conn.execute(f"UPDATE items SET {sets}, embedding = NULL WHERE key = %(key)s", values)
    _write_creators(conn, preprint["key"], record.get("creators") or [])


def _candidates(conn, title_norm: str, relation: dict | None) -> list[dict]:
    """Held items that could be another version: the same normalised title,
    or an identifier the registry's relation links name."""
    rows = conn.execute("SELECT * FROM items WHERE title_norm = %s", (title_norm,)).fetchall()
    seen = {row["key"] for row in rows}
    for links in (relation or {}).values():
        for link in links:
            ident = own_identifiers({"DOI": link.get("id", "")})
            key = holder(conn, ident) if ident else None
            if key and key not in seen:
                seen.add(key)
                rows.append(conn.execute("SELECT * FROM items WHERE key = %s", (key,)).fetchone())
    return rows


def admit(conn: psycopg.Connection, record: dict, provenance_kind: str,
          member: int | None = None, *, relation: dict | None = None,
          publisher: str | None = None, category: str | None = None) -> tuple[str, bool]:
    """Admit a publication: (the item's key, whether a new item was made).

    `relation` is the registry's relation links for the record (Crossref's
    "is-preprint-of" / "has-preprint"), tried before the title when looking
    for another version. `publisher` and `category` place the item when the
    caller knows them. Provenance is set when an item is created and never
    changed afterwards. ValueError for a record without a type or title."""
    if provenance_kind not in PROVENANCE:
        raise ValueError(f"provenance must be one of {', '.join(PROVENANCE)}")
    title_norm = normalise_title(record.get("title", ""))
    if not record.get("itemType") or not title_norm:
        raise ValueError("an item needs a type and a title")
    own = own_identifiers(record)
    with conn.transaction():
        _lock(conn, [*own, f"title:{title_norm}"])
        if key := holder(conn, own):
            return key, False
        index = VersionIndex([to_record(row) for row in _candidates(conn, title_norm, relation)])
        match = index.other_version(record, {"relation": relation} if relation else None)
        if match is not None:
            if not is_preprint(record):
                _replace(conn, match.item, record, publisher, category)
                return match.item["key"], False
            ident, line = record_line(record)
            if ident:
                _record_preprint(conn, match.item, ident, line)
                return match.item["key"], False
        return insert(conn, record, provenance_kind, member, publisher, category), True


def get(conn: psycopg.Connection, key: str) -> dict | None:
    """The items row for a key, or None."""
    return conn.execute("SELECT * FROM items WHERE key = %s", (key,)).fetchone()
