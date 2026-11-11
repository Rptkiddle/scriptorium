"""Retrieval: what the find and inspect requests return.

Every function takes a connection and plain inputs and returns a dict
ready to be sent. The caller says how many results, which fields and in
what order; nothing here keeps results between requests. No reply holds a
member's name or number: provenance is given by its kind only.

Vector searches pass the query vector as a value, which is what lets the
database use its vector index, and scan the index on until enough rows pass
the filters. A search within given item keys (a set sent back by the
client) compares against every one of those items instead, so it is exact.
"""

import re
from collections.abc import Callable

import psycopg

from scriptorium_server import items as collection
from scriptorium_server.embedding import Embedder, vector_literal
from scriptorium_server.identity import author_names, classify, query_key
from scriptorium_server.sources.crossref import RegistryUnavailable

FILTERS = ("authors", "authors_all", "years", "year_from", "year_to", "venues", "publishers",
           "categories", "types", "has_fulltext", "added_since", "provenance", "item_keys")
FIELDS = ("key", "citation_key", "type", "title", "authors", "year", "date", "venue",
          "publisher", "category", "doi", "arxiv_id", "abstract", "has_fulltext", "provenance",
          "added_at", "preprints", "details")
DEFAULT_FIELDS = ("key", "citation_key", "type", "title", "authors", "year", "venue", "doi",
                  "has_fulltext")
ORDERS = {"added": "i.added_at DESC, i.key", "year": "i.year DESC NULLS LAST, i.key",
          "title": "i.title_norm, i.key"}
FACETS = {"year": "i.year::text", "venue": "v.name", "publisher": "i.publisher",
          "category": "i.category", "type": "i.type", "provenance": "i.provenance_kind",
          "has_fulltext": "i.has_fulltext::text"}
BIB_FORMATS = ("biblatex", "bibtex")
_FROM = "FROM items i LEFT JOIN venues v ON v.id = i.venue_id"


class BadRequest(ValueError):
    """The request cannot be served as asked; the message says why."""


# --- filters and fields --------------------------------------------------------


def _lower(values: list[str]) -> list[str]:
    return [str(v).strip().lower() for v in values]


def where(filters: dict | None) -> tuple[str, list]:
    """The SQL condition (on items as `i`) and its values for a filters
    dict. Values within one filter match any; different filters must all
    hold. BadRequest for a filter that does not exist."""
    filters = {k: v for k, v in (filters or {}).items() if v is not None and v != []}
    if unknown := sorted(set(filters) - set(FILTERS)):
        raise BadRequest(f"unknown filter(s): {', '.join(unknown)}; the filters are"
                         f" {', '.join(FILTERS)}")
    sql, values = ["TRUE"], []
    by_author = "EXISTS (SELECT 1 FROM item_creators c WHERE c.item = i.key AND c.name_key = {})"
    if "authors" in filters:
        sql.append(by_author.format("ANY(%s)"))
        values.append([query_key(a) for a in filters["authors"]])
    for author in filters.get("authors_all", []):
        sql.append(by_author.format("%s"))
        values.append(query_key(author))
    simple = {"years": "i.year = ANY(%s)", "year_from": "i.year >= %s", "year_to": "i.year <= %s",
              "types": "i.type = ANY(%s)", "has_fulltext": "i.has_fulltext = %s",
              "added_since": "i.added_at >= %s::date", "provenance": "i.provenance_kind = ANY(%s)",
              "item_keys": "i.key = ANY(%s)"}
    for name, condition in simple.items():
        if name in filters:
            sql.append(condition)
            values.append(filters[name])
    lowered = {"venues": "i.venue_id IN (SELECT id FROM venues WHERE lower(name) = ANY(%s))",
               "publishers": "lower(i.publisher) = ANY(%s)",
               "categories": "lower(i.category) = ANY(%s)"}
    for name, condition in lowered.items():
        if name in filters:
            sql.append(condition)
            values.append(_lower(filters[name]))
    return " AND ".join(sql), values


def _fields(fields: list[str] | None) -> tuple[str, ...]:
    chosen = tuple(fields) if fields else DEFAULT_FIELDS
    if unknown := sorted(set(chosen) - set(FIELDS)):
        raise BadRequest(f"unknown field(s): {', '.join(unknown)}; the fields are"
                         f" {', '.join(FIELDS)}")
    return chosen


def _item(row: dict, fields: tuple[str, ...]) -> dict:
    """An items row (joined with its venue's name) as the chosen fields."""
    full = {
        "key": row["key"], "citation_key": row["citation_key"], "type": row["type"],
        "title": row["title"], "authors": author_names(row["creators"]), "year": row["year"],
        "date": row["date"], "venue": row["venue"], "publisher": row["publisher"],
        "category": row["category"], "doi": row["doi"], "arxiv_id": row["arxiv_id"],
        "abstract": row["abstract"], "has_fulltext": row["has_fulltext"],
        "provenance": row["provenance_kind"],
        "added_at": row["added_at"].date().isoformat(),
        "preprints": row["other_ids"], "details": row["data"],
    }
    return {name: full[name] for name in fields}


_COLUMNS = ("i.key, i.citation_key, i.type, i.title, i.creators, i.year, i.date, v.name AS venue,"
            " i.publisher, i.category, i.doi, i.arxiv_id, i.abstract, i.has_fulltext,"
            " i.provenance_kind, i.added_at, i.other_ids, i.data")


# --- list, item, facets --------------------------------------------------------


def list_items(conn: psycopg.Connection, filters: dict | None = None, count: int = 20,
               offset: int = 0, order: str = "added", fields: list[str] | None = None) -> dict:
    """Items by filters only: {"total", "items"}. count=0 gives the total alone."""
    if order not in ORDERS:
        raise BadRequest(f"order must be one of {', '.join(ORDERS)}")
    chosen = _fields(fields)
    condition, values = where(filters)
    total = conn.execute(f"SELECT count(*) AS n {_FROM} WHERE {condition}", values).fetchone()["n"]
    rows = conn.execute(f"SELECT {_COLUMNS} {_FROM} WHERE {condition} ORDER BY {ORDERS[order]}"
                        " LIMIT %s OFFSET %s", [*values, count, offset]).fetchall() if count else []
    return {"total": total, "items": [_item(r, chosen) for r in rows]}


def item(conn: psycopg.Connection, key: str) -> dict | None:
    """One item in full, by item key or citation key, with how many
    passages it has and whether its PDF is held; None when not held."""
    row = conn.execute(f"SELECT {_COLUMNS} {_FROM} WHERE i.key = %s OR i.citation_key = %s"
                       " ORDER BY (i.key = %s) DESC LIMIT 1", (key, key, key)).fetchone()
    if row is None:
        return None
    extra = conn.execute(
        "SELECT (SELECT count(*) FROM passages WHERE item = %(k)s) AS passages,"
        " EXISTS (SELECT 1 FROM files WHERE item = %(k)s) AS pdf_held", {"k": row["key"]}).fetchone()
    return {**_item(row, FIELDS), "creators": row["creators"], **extra}


def facets(conn: psycopg.Connection, field: str, filters: dict | None = None,
           contains: str = "", count: int = 50) -> dict:
    """The values a filter can take, with how many items have each, most
    frequent first: {"field", "values": [{"value", "items"}], "more"}."""
    condition, values = where(filters)
    if field == "author":
        sql = (f"SELECT c.name_key AS value, count(DISTINCT c.item) AS n {_FROM}"
               f" JOIN item_creators c ON c.item = i.key WHERE {condition}"
               " AND c.name_key LIKE %s GROUP BY 1")
    elif field in FACETS:
        sql = (f"SELECT {FACETS[field]} AS value, count(*) AS n {_FROM} WHERE {condition}"
               f" AND {FACETS[field]} IS NOT NULL AND lower({FACETS[field]}) LIKE %s GROUP BY 1")
    else:
        raise BadRequest(f"field must be one of {', '.join([*FACETS, 'author'])}")
    pattern = "%" + re.sub(r"([\\%_])", r"\\\1", contains.strip().lower()) + "%"
    rows = conn.execute(sql + " ORDER BY n DESC, value LIMIT %s",
                        [*values, pattern, count + 1]).fetchall()
    return {"field": field, "values": [{"value": r["value"], "items": r["n"]} for r in rows[:count]],
            "more": len(rows) > count}


# --- the two searches ----------------------------------------------------------


def _use_index(conn: psycopg.Connection, count: int) -> None:
    """For this transaction: scan the vector index on until enough rows
    pass the filters, keeping exact order of distance."""
    conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
    conn.execute(f"SET LOCAL hnsw.ef_search = {min(max(int(count) * 2, 100), 1000)}")


def _phrase(phrase: str | None, column: str) -> tuple[str, list]:
    if not (phrase or "").strip():
        return "", []
    return f" AND {column} @@ phraseto_tsquery('simple', %s)", [phrase.strip()]


def search_items(conn: psycopg.Connection, embedder: Embedder, query: str,
                 filters: dict | None = None, count: int = 20,
                 fields: list[str] | None = None, phrase: str | None = None) -> dict:
    """Items whose title and abstract are nearest the query, within the
    filters and, when given, holding the exact phrase: {"items"}, each with
    a "score" (cosine similarity, 1 = identical)."""
    if not (query or "").strip():
        raise BadRequest("a search needs a query")
    chosen = _fields(fields)
    condition, values = where(filters)
    keyword, keyword_values = _phrase(phrase, "i.tsv")
    vector = vector_literal(embedder.embed_query(query, "items"))
    condition += keyword + " AND i.embedding IS NOT NULL"
    values += keyword_values
    if (filters or {}).get("item_keys"):
        sql = (f"WITH chosen AS MATERIALIZED (SELECT {_COLUMNS}, i.embedding {_FROM}"
               f" WHERE {condition}) SELECT *, embedding <=> %s::halfvec AS distance"
               " FROM chosen ORDER BY distance LIMIT %s")
        rows = conn.execute(sql, [*values, vector, count]).fetchall()
    else:
        _use_index(conn, count)
        sql = (f"SELECT {_COLUMNS}, i.embedding <=> %s::halfvec AS distance {_FROM}"
               f" WHERE {condition} ORDER BY distance LIMIT %s")
        rows = conn.execute(sql, [vector, *values, count]).fetchall()
    return {"items": [{**_item(r, chosen), "score": round(1 - r["distance"], 4)} for r in rows]}


def search_passages(conn: psycopg.Connection, embedder: Embedder, query: str,
                    filters: dict | None = None, count: int = 10,
                    phrase: str | None = None) -> dict:
    """Passages of held full texts nearest the query, within the item
    filters and, when given, holding the exact phrase: {"passages"}, each
    with its text, part, pages, score and the item it belongs to."""
    if not (query or "").strip():
        raise BadRequest("a search needs a query")
    condition, values = where(filters)
    keyword, keyword_values = _phrase(phrase, "p.tsv")
    vector = vector_literal(embedder.embed_query(query, "passages"))
    condition += keyword + " AND p.embedding IS NOT NULL"
    values += keyword_values
    columns = ("p.item, p.idx, p.part, p.pages, p.text, i.citation_key, i.title, i.year,"
               " i.creators")
    source = "FROM passages p JOIN items i ON i.key = p.item"
    if (filters or {}).get("item_keys"):
        sql = (f"WITH chosen AS MATERIALIZED (SELECT {columns}, p.embedding {source}"
               f" WHERE {condition}) SELECT *, embedding <=> %s::halfvec AS distance"
               " FROM chosen ORDER BY distance LIMIT %s")
        rows = conn.execute(sql, [*values, vector, count]).fetchall()
    else:
        _use_index(conn, count)
        sql = (f"SELECT {columns}, p.embedding <=> %s::halfvec AS distance {source}"
               f" WHERE {condition} ORDER BY distance LIMIT %s")
        rows = conn.execute(sql, [vector, *values, count]).fetchall()
    return {"passages": [{
        "item": r["item"], "passage": r["idx"], "part": r["part"], "pages": r["pages"],
        "text": r["text"], "score": round(1 - r["distance"], 4),
        "citation_key": r["citation_key"], "title": r["title"], "year": r["year"],
        "authors": author_names(r["creators"])} for r in rows]}


# --- lookup --------------------------------------------------------------------

Registry = Callable[[str, str], tuple[dict, dict | None] | None]


def _brief(conn: psycopg.Connection, key: str) -> dict:
    row = conn.execute(f"SELECT {_COLUMNS} {_FROM} WHERE i.key = %s", (key,)).fetchone()
    return _item(row, DEFAULT_FIELDS)


def lookup(conn: psycopg.Connection, inputs: list[str], member: int | None = None,
           registry: Registry | None = None) -> dict:
    """Exact lookups: {"results"}, one per input, in order. A DOI or arXiv
    ID finds the item that answers to it, as its own identifier or as a
    recorded preprint. One that is not held is asked of the registry and,
    when the registry knows it, admitted with provenance "activity". A
    title finds every item with that exact title (after normalisation)."""
    results = []
    for text in inputs:
        kind, value = classify(text)
        result: dict = {"input": text, "kind": kind, "found": False, "items": []}
        if kind == "title":
            rows = conn.execute("SELECT key FROM items WHERE title_norm = %s ORDER BY key",
                                (value,)).fetchall() if value else []
            result["items"] = [_brief(conn, r["key"]) for r in rows]
            result["found"] = bool(rows)
        elif key := collection.holder(conn, [f"{kind}:{value}"]):
            held = _brief(conn, key)
            own = (held["doi"] or "").lower() == value or kind == "arxiv" and conn.execute(
                "SELECT 1 FROM items WHERE key = %s AND lower(arxiv_id) = %s", (key, value)).fetchone()
            result.update(found=True, items=[held])
            if not own:
                result["note"] = "held as the published version of this preprint"
        elif registry is None:
            result["note"] = "not held"
        else:
            try:
                known = registry(kind, value)
            except RegistryUnavailable:
                result["note"] = ("not held; the registry could not be reached, so nothing"
                                  " was added")
            else:
                if known is None:
                    result["note"] = "not held, and the registry does not know this identifier"
                else:
                    record, relation = known
                    key, created = collection.admit(conn, record, "activity", member,
                                                    relation=relation)
                    result.update(found=True, items=[_brief(conn, key)])
                    result["note"] = ("added to the collection from the registry" if created
                                      else "held as another version of this publication")
        results.append(result)
    return {"results": results}


# --- BibTeX --------------------------------------------------------------------

_ENTRY_TYPES = {"journalArticle": ("article", "article"),
                "conferencePaper": ("inproceedings", "inproceedings"),
                "preprint": ("online", "misc"), "book": ("book", "book"),
                "bookSection": ("incollection", "incollection"),
                "presentation": ("unpublished", "unpublished")}
_CONTAINER = ("publicationTitle", "proceedingsTitle", "bookTitle")
_LATEX = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
          "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
          "^": r"\textasciicircum{}"}


def _tex(text) -> str:
    return "".join(_LATEX.get(ch, ch) for ch in " ".join(str(text).split()))


def _bib_names(creators: list[dict]) -> str:
    authors = [c for c in creators if c.get("creatorType", "author") == "author"] or creators
    names = []
    for c in authors:
        if c.get("lastName"):
            first = f", {_tex(c['firstName'])}" if c.get("firstName") else ""
            names.append(f"{_tex(c['lastName'])}{first}")
        elif c.get("name"):
            names.append("{" + _tex(c["name"]) + "}")
    return " and ".join(names)


def bib_entry(row: dict, fmt: str, abstract: bool) -> str:
    """One items row as a BibLaTeX or BibTeX entry, keyed by citation key."""
    data, biblatex = row["data"], fmt == "biblatex"
    entry_type = _ENTRY_TYPES.get(row["type"], ("misc", "misc"))[0 if biblatex else 1]
    container = next((data[f] for f in _CONTAINER if data.get(f)), "")
    fields = [("author", _bib_names(row["creators"])), ("title", "{" + _tex(row["title"]) + "}")]
    if container:
        name = ("journaltitle" if biblatex else "journal") if row["type"] == "journalArticle" \
            else "booktitle"
        fields.append((name, _tex(container)))
    if biblatex and row["date"]:
        fields.append(("date", _tex(row["date"])))
    elif row["year"]:
        fields.append(("year", str(row["year"])))
    for name, source in (("volume", "volume"), ("number", "issue"), ("publisher", "publisher")):
        if data.get(source):
            fields.append((name, _tex(data[source])))
    if data.get("pages"):
        fields.append(("pages", _tex(re.sub(r"\s*[-–—]+\s*", "--", str(data["pages"])))))
    if row["doi"]:
        fields.append(("doi", row["doi"]))
    if row["arxiv_id"]:
        fields += [("eprint", row["arxiv_id"]),
                   ("eprinttype" if biblatex else "archiveprefix", "arXiv")]
    if data.get("url") and not row["doi"]:
        fields.append(("url", data["url"]))
    if abstract and row["abstract"]:
        fields.append(("abstract", _tex(row["abstract"])))
    body = "".join(f"\t{name} = {{{value}}},\n" for name, value in fields if value)
    return f"@{entry_type}{{{row['citation_key'] or row['key']},\n{body}}}\n"


def bibtex(conn: psycopg.Connection, keys: list[str], fmt: str = "biblatex",
           abstracts: bool = False) -> dict:
    """Entries for the chosen items (item or citation keys), in the order
    asked: {"format", "entries", "bibtex", "missing"}."""
    if fmt not in BIB_FORMATS:
        raise BadRequest(f"format must be one of {', '.join(BIB_FORMATS)}")
    keys = list(dict.fromkeys(keys))
    rows = conn.execute("SELECT * FROM items WHERE key = ANY(%(k)s) OR citation_key = ANY(%(k)s)",
                        {"k": keys}).fetchall()
    by_key = {r["key"]: r for r in rows} | {r["citation_key"]: r for r in rows if r["citation_key"]}
    found = [by_key[k] for k in keys if k in by_key]
    return {"format": fmt, "entries": len(found),
            "bibtex": "\n".join(bib_entry(r, fmt, abstracts) for r in found),
            "missing": [k for k in keys if k not in by_key]}


def collection_size(conn: psycopg.Connection) -> dict:
    row = conn.execute("SELECT count(*) AS items, count(*) FILTER (WHERE has_fulltext)"
                       " AS fulltexts FROM items").fetchone()
    return dict(row)
