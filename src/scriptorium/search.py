"""Retrieval: dense search over the index.

Unfiltered searches match PaperQA2's default retrieval exactly: cosine
similarity top-k (PaperQA2 ships MMR but disables it by default via
mmr_lambda=1.0). Optional metadata filters (a scriptorium extension; PaperQA2
scopes retrieval by agentic paper selection instead) restrict the candidate
pool before similarity ranking. Relevance scoring of retrieved chunks
(PaperQA2's RCS step, an LLM task) is performed by Claude in-session per the
scriptorium skill — not here.

Search is exact: the query vector is compared with every admitted vector
(scriptorium.store.VectorMatrix). The vectors are loaded into memory on
the first search and released when idle (the daemon's reaper); filters,
listings, facets and lookups are SQL over the store.
"""

import datetime
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from scriptorium.config import Config
from scriptorium.identifiers import classify
from scriptorium.index import Embedder, HubIndex, check_embedder
from scriptorium.names import query_key
from scriptorium.store import FilterSQL, VectorMatrix, any_value

# Claude Code saves an MCP reply longer than 50,000 characters (or 25,000
# tokens; this JSON runs ~2.5 characters per token) to a file instead of
# returning it. Measured replies on the library: 50 listed items ~25,000
# characters, 40 search hits ~27,000, 15 hits with abstracts ~32,000. The caps
# keep replies near 30,000 characters; the tools also declare a higher
# per-tool limit as a margin (tools.REPLY_LIMIT_META).
LIST_LIMIT_MAX = 60
SEARCH_K_MAX = 40
ABSTRACTS_MAX = 15
ABSTRACT_CHARS = 2000
AUTHORS_SHOWN = 10
# Filter keys chunk search accepts (the rest are for item search and listing).
CHUNK_FILTER_KEYS = {"creators", "creators_all", "years", "year_from", "year_to", "venue",
                     "citation_keys", "item_keys"}
ITEM_FILTER_KEYS = CHUNK_FILTER_KEYS | {"tags", "item_types", "collections", "has_pdf",
                                        "categories", "publishers", "venue_folders",
                                        "added_since"}
_LIST_FIELDS = ["item_key", "citation_key", "citation", "authors", "item_type", "year",
                "year_int", "venue_folders", "doi", "has_pdf", "date_added", "added_ts"]
# While a sync is writing, a search may use vectors loaded before it began;
# a chunk replaced meanwhile is then missing, so a few extra are scored.
_STALE_MARGIN = 20


def _result_fields(payload: dict) -> dict:
    """The fields every item result carries: who (first authors with given
    names, and how many there are) and when added, beside the citation."""
    authors = payload.get("authors") or []
    return {"authors": authors[:AUTHORS_SHOWN], "author_count": len(authors),
            "date_added": (payload.get("date_added") or "")[:10]}


def _abstract(payload: dict) -> str:
    text = " ".join((payload.get("abstract") or "").split())
    return text if len(text) <= ABSTRACT_CHARS else text[:ABSTRACT_CHARS].rstrip() + " [...]"


_LIST_KEYS = {"creators", "creators_all", "years", "item_types", "tags", "collections",
              "categories", "publishers", "venue_folders", "citation_keys", "item_keys"}


def _year(key: str, value) -> int:
    if isinstance(value, bool) or not (isinstance(value, int) or str(value).strip().isdigit()):
        raise ValueError(f'filter "{key}" takes years as whole numbers, e.g. 2020; got {value!r}')
    return int(value)


def _checked(filters: dict) -> dict:
    """Filters with their value shapes checked: lists where lists are
    expected (a single string is refused rather than guessed at, since
    "Kroon, Trilling" would otherwise read as one surname), years as whole
    numbers (numeric strings accepted), has_pdf as true/false, added_since
    as YYYY-MM-DD, venue as text."""
    out = dict(filters)
    for key, value in filters.items():
        if value is None:
            continue
        if key in _LIST_KEYS and not isinstance(value, (list, tuple)):
            raise ValueError(f'filter "{key}" takes a list, e.g. "{key}": [{json.dumps(value)}]')
        if key == "years":
            out[key] = [_year(key, y) for y in value]
        elif key in ("year_from", "year_to"):
            out[key] = _year(key, value)
        elif key == "has_pdf" and not isinstance(value, bool):
            raise ValueError('filter "has_pdf" takes true or false')
        elif key == "added_since":
            try:
                datetime.date.fromisoformat(str(value))
            except ValueError:
                raise ValueError('filter "added_since" takes a date as "YYYY-MM-DD"') from None
        elif key == "venue" and not isinstance(value, str):
            raise ValueError('filter "venue" takes one venue name as text')
    return out


def _in(column: str, values: list) -> tuple[str, list]:
    return f"{column} IN ({', '.join('?' * len(values))})", list(values)


def build_filter(filters: dict | None,
                 allowed: set[str] = ITEM_FILTER_KEYS) -> FilterSQL | None:
    """Translate a filters dict into SQL over the catalogue's items. A key
    outside `allowed` raises ValueError naming the valid keys, so a
    mistyped filter never widens a search without notice.

    Supported keys (all optional, AND-combined):
      creators:   list of surnames, any form ("van Atteveldt", "Atteveldt, W.";
                  case, accents and particles ignored; any match)
      creators_all: the same, but every listed author must be on the item
      years:      list of years (ints; any match)
      year_from / year_to: inclusive year range
      venue:      venue name (case-insensitive exact match)
      citation_keys / item_keys: explicit paper lists (any match)

    Item-search keys (hub_search_items, hub_list_items; chunk search
    refuses them):
      tags:       list of tags (any match)
      item_types: list of Zotero item types (any match)
      collections: list of collection keys (any match)
      has_pdf:    bool
      categories / publishers / venue_folders: folder names from the
                  library tree, category > publisher > venue, as shown in
                  Zotero without the [✓]/[✗] mark (case-insensitive; any match)
      added_since: "YYYY-MM-DD", items added to Zotero on or after that day
    """
    if not filters:
        return None
    if unknown := set(filters) - allowed:
        hint = ("; it applies to item search (hub_search_items, hub_list_items) only"
                if unknown <= ITEM_FILTER_KEYS else "")
        raise ValueError(f"unknown filter {', '.join(sorted(unknown))}{hint};"
                         f" valid filters here: {', '.join(sorted(allowed))}")
    filters = _checked(filters)
    parts: list[tuple[str, list]] = []
    if v := filters.get("tags"):
        parts.append(any_value("tag", list(v), column="value"))
    if v := filters.get("item_types"):
        parts.append(_in("i.item_type", list(v)))
    if v := filters.get("collections"):
        parts.append(any_value("collection", list(v), column="value"))
    if (v := filters.get("has_pdf")) is not None:
        parts.append(("i.has_pdf = ?", [int(bool(v))]))
    if v := filters.get("creators"):
        parts.append(any_value("creator", [query_key(s) for s in v], column="value"))
    for name in filters.get("creators_all") or []:
        parts.append(any_value("creator", [query_key(name)], column="value"))
    if v := filters.get("years"):
        parts.append(_in("i.year_int", [int(y) for y in v]))
    if filters.get("year_from") is not None:
        parts.append(("i.year_int >= ?", [filters["year_from"]]))
    if filters.get("year_to") is not None:
        parts.append(("i.year_int <= ?", [filters["year_to"]]))
    if v := filters.get("venue"):
        parts.append(("i.venue_lc = ?", [v.lower()]))
    for key, field in (("categories", "category"), ("publishers", "publisher"),
                       ("venue_folders", "venue_folder")):
        if v := filters.get(key):
            parts.append(any_value(field, [s.lower() for s in v]))
    if v := filters.get("added_since"):
        since = datetime.datetime.fromisoformat(v).replace(tzinfo=datetime.UTC)
        parts.append(("i.added_ts >= ?", [int(since.timestamp())]))
    if v := filters.get("citation_keys"):
        parts.append(_in("i.citation_key", list(v)))
    if v := filters.get("item_keys"):
        parts.append(_in("i.item_key", list(v)))
    if not parts:
        return None
    return FilterSQL(" AND ".join(f"({sql})" for sql, _ in parts),
                     [p for _, params in parts for p in params])


@dataclass
class SearchResult:
    rank: int
    citation_key: str
    citation: str
    name: str  # docname + page range, e.g. "cinelli2021 pages 3-5"
    text: str
    similarity: float
    item_key: str
    chunk_index: int

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "citation_key": self.citation_key,
            "citation": self.citation,
            "name": self.name,
            "similarity": round(self.similarity, 4),
            "item_key": self.item_key,
            "chunk_index": self.chunk_index,
            "text": self.text,
        }


class HubSearch:
    """Query interface over the scriptorium index.

    `hub` is the index to read (the daemon passes its own, so searches and
    syncs share one store); without one, a HubIndex is opened. `writing`
    tells whether a sync is writing right now: while it is, a search uses
    the vectors already loaded rather than reloading after every paper."""

    def __init__(self, config: Config, hub: HubIndex | None = None,
                 embedder: Embedder | None = None,
                 writing: Callable[[], bool] | None = None):
        self.config = config
        self.embedder = embedder or (hub.embedder if hub else
                                     Embedder(config.embedding_model, config.embedding_dim))
        self.hub = hub or HubIndex(config, embedder=self.embedder)
        self.store = self.hub.store
        self._writing = writing or (lambda: False)
        self._matrices: dict[str, VectorMatrix] = {}
        self._load_lock = threading.Lock()
        self._revision_checked = False
        self.last_used = 0.0

    # -- the vectors in memory -------------------------------------------

    @property
    def loaded(self) -> bool:
        return bool(self._matrices)

    def release(self) -> None:
        """Free the in-memory vectors; the next search loads them again."""
        self._matrices = {}

    def _matrix(self, kind: str) -> VectorMatrix:
        """The vectors of "chunks" or "items", loaded if absent or stale."""
        self.last_used = time.monotonic()
        current = self.store.generation[kind]
        m = self._matrices.get(kind)
        if m is not None and (m.generation == current or self._writing()):
            return m
        with self._load_lock:
            m = self._matrices.get(kind)
            if m is not None and m.generation == current:
                return m
            # Chunk results are read back by chunk id; item results by item key
            # (an item's rowid is not used).
            sql = ("SELECT id, item_key, vec FROM chunks ORDER BY id" if kind == "chunks"
                   else "SELECT rowid, item_key, vec FROM items ORDER BY rowid")
            m = VectorMatrix.load(self.store.query(sql), self.config.embedding_dim, current)
            self._matrices[kind] = m
            return m

    def _query_vector(self, encode: Callable[[str], list[float]], text: str) -> list[float]:
        if not self._revision_checked:
            check_embedder(self.store, self.embedder)
            self._revision_checked = True
        return encode(text)

    def _admitted(self, flt: FilterSQL | None) -> set[str] | None:
        """The item keys a filter admits; None when there is no filter."""
        if flt is None:
            return None
        return {r[0] for r in self.store.query(*flt.items_sql())}

    # -- searches ----------------------------------------------------------

    def search(self, query: str, k: int = 10, filters: dict | None = None) -> list[SearchResult]:
        flt = build_filter(filters, CHUNK_FILTER_KEYS)
        query_vector = self._query_vector(self.embedder.encode_query, query)
        m = self._matrix("chunks")
        if not len(m.ids):
            raise RuntimeError("Index is empty — run a sync first.")
        writing = self._writing()
        best = m.top(query_vector, k + (_STALE_MARGIN if writing else 0),
                     m.rows_of(self._admitted(flt)))
        ids = [int(m.ids[row]) for row, _ in best]
        score = {int(m.ids[row]): s for row, s in best}
        marks = ", ".join("?" * len(ids))
        rows = {r["id"]: r for r in self.store.query(
            "SELECT c.id, c.item_key, c.chunk_index, c.pages, c.text, i.citation_key,"
            " json_extract(i.payload, '$.citation') AS citation"
            f" FROM chunks c LEFT JOIN items i ON i.item_key = c.item_key WHERE c.id IN ({marks})",
            ids)} if ids else {}
        results = []
        for cid in ids:
            r = rows.get(cid)
            if r is None:  # replaced by a sync since the vectors were loaded
                continue
            docname = r["citation_key"] or r["item_key"]
            results.append(SearchResult(
                rank=len(results) + 1, citation_key=docname, citation=r["citation"] or "",
                name=f"{docname} pages {r['pages']}" if r["pages"] else docname,
                text=r["text"], similarity=score[cid], item_key=r["item_key"],
                chunk_index=r["chunk_index"]))
            if len(results) == k:
                break
        return results

    def facets(self, field: str, contains: str = "", scope: str = "fulltext",
               filters: dict | None = None) -> dict:
        """Distinct values of a metadata field, with per-paper counts.
        scope "fulltext": across the papers with indexed full text (fields
        creators | venues | years). scope "items": across the whole
        catalogue (every item), which adds the fields categories |
        publishers | venue_folders | item_types. `filters` (build_filter's
        schema) counts within a filtered set; in scope "fulltext" only the
        chunk keys are allowed. Raises ValueError on an unknown scope,
        field or filter key."""
        # field -> a multi-value field name, or an SQL expression over items
        fields = {"creators": ("creator", None), "venues": (None, "i.venue_lc"),
                  "years": (None, "json_extract(i.payload, '$.year')")}
        if scope == "items":
            fields |= {"categories": ("category", None), "publishers": ("publisher", None),
                       "venue_folders": ("venue_folder", None),
                       "item_types": (None, "i.item_type")}
        elif scope != "fulltext":
            raise ValueError("scope must be fulltext or items")
        if field not in fields:
            raise ValueError(f"field must be one of: {', '.join(fields)}")
        if scope == "fulltext" and (bad := set(filters or {}) - CHUNK_FILTER_KEYS):
            raise ValueError(f"filters {sorted(bad)} apply to scope 'items' only; scope"
                             f" 'fulltext' takes {sorted(CHUNK_FILTER_KEYS)}")
        flt = build_filter(filters, ITEM_FILTER_KEYS if scope == "items" else CHUNK_FILTER_KEYS)
        where = [flt.where] if flt and flt.where else []
        params = list(flt.params) if flt else []
        if scope == "fulltext":
            where.append("EXISTS (SELECT 1 FROM papers p WHERE p.item_key = i.item_key)")
        multi, expr = fields[field]
        if multi:
            sql = ("SELECT v.value AS value, COUNT(DISTINCT i.item_key) AS n FROM items i"
                   " JOIN item_values v ON v.item_key = i.item_key AND v.field = ?")
            params = [multi, *params]
        else:
            sql = f"SELECT {expr} AS value, COUNT(*) AS n FROM items i"
        sql += (" WHERE " + " AND ".join(f"({w})" for w in where) if where else "")
        sql += " GROUP BY value"
        needle = (query_key(contains) or contains.lower()) if field == "creators" else contains.lower()
        counts = {str(r["value"]): r["n"] for r in self.store.query(sql, params)
                  if r["value"] not in (None, "")
                  and (not needle or needle in str(r["value"]).lower())}
        return dict(sorted(counts.items()))

    def papers(self, filters: dict | None = None) -> list[dict]:
        """The papers a filter set admits (the retrieval pool): one record per
        paper with indexed full text, with citation_key and short-form
        citation, sorted by key."""
        flt = build_filter(filters, CHUNK_FILTER_KEYS) or FilterSQL("", [])
        sql, params = flt.items_sql(
            "i.item_key, i.citation_key, json_extract(i.payload, '$.citation') AS citation")
        sql += (" AND" if flt.where else " WHERE") + \
            " EXISTS (SELECT 1 FROM papers p WHERE p.item_key = i.item_key)"
        papers = [{"citation_key": r["citation_key"] or r["item_key"], "citation": r["citation"]}
                  for r in self.store.query(sql, params)]
        return sorted(papers, key=lambda r: r["citation_key"].lower())

    def _payloads(self, item_keys: list[str]) -> dict[str, dict]:
        if not item_keys:
            return {}
        marks = ", ".join("?" * len(item_keys))
        return {r["item_key"]: json.loads(r["payload"]) for r in self.store.query(
            f"SELECT item_key, payload FROM items WHERE item_key IN ({marks})", item_keys)}

    def search_items(
        self, query: str, k: int = 20, filters: dict | None = None, abstracts: bool = False
    ) -> list[dict]:
        """Item-level semantic search over the catalogue (the whole library,
        metadata embeddings). Returns one record per item, best first; with
        abstracts=True each carries its abstract (k at most ABSTRACTS_MAX)."""
        if abstracts and k > ABSTRACTS_MAX:
            raise ValueError(f"with abstracts, k must be at most {ABSTRACTS_MAX}")
        flt = build_filter(filters)
        query_vector = self._query_vector(self.embedder.encode_item_query, query)
        m = self._matrix("items")
        if not len(m.ids):
            raise RuntimeError("Catalogue is empty — run a sync first.")
        best = m.top(query_vector, k, m.rows_of(self._admitted(flt)))
        keys = [m.items[row] for row, _ in best]
        payloads = self._payloads(keys)
        results = []
        for (row, sim), key in zip(best, keys, strict=True):
            p = payloads.get(key)
            if p is None:  # removed by a sync since the vectors were loaded
                continue
            results.append({
                "rank": len(results) + 1,
                "similarity": round(sim, 4),
                "item_key": p["item_key"],
                "item_type": p["item_type"],
                "citation_key": p["citation_key"],
                "citation": p["citation"],
                "year": p["year"],
                "venue": p.get("venue", ""),
                "venue_folders": p.get("venue_folders", []),
                "doi": p.get("doi", ""),
                "tags": p["tags"],
                "has_pdf": p["has_pdf"],
                **_result_fields(p),
                **({"abstract": _abstract(p)} if abstracts else {}),
            })
        return results

    def lookup(self, inputs: list[str], candidates: int = 3) -> list[dict]:
        """Exact lookup of library items by DOI, arXiv ID (bare, arXiv: or
        URL forms, any version) or exact title. One entry per input, with
        every item that matches and how: its own DOI or arXiv ID, a preprint
        recorded in its Extra field, or its title. An unmatched title also
        gets the closest items by topic search, as candidates."""
        results = []
        for raw in inputs:
            kind, value = classify(raw)
            if kind == "title":
                rows = self.store.query(
                    "SELECT payload FROM items WHERE title_norm = ? LIMIT 50", (value,))
            else:
                rows = self.store.query(
                    "SELECT payload FROM items i WHERE EXISTS (SELECT 1 FROM item_values v"
                    " WHERE v.item_key = i.item_key AND v.field IN"
                    " ('identifier', 'preprint_identifier') AND v.value = ?) LIMIT 50",
                    (f"{kind}:{value}",))
            matches = []
            for row in rows:
                p = json.loads(row["payload"])
                if kind == "title":
                    how = "title"
                elif f"{kind}:{value}" in p.get("identifiers", []):
                    how = kind
                else:
                    how = "recorded preprint of this item"
                matches.append({
                    "item_key": p["item_key"],
                    "citation_key": p["citation_key"],
                    "citation": p["citation"],
                    "item_type": p["item_type"],
                    "doi": p.get("doi", ""),
                    "has_pdf": p["has_pdf"],
                    "matched_on": how,
                })
            entry = {"input": raw, "kind": kind, "found": bool(matches), "matches": matches}
            if not matches and kind == "title" and candidates:
                entry["candidates"] = [
                    {k: h[k] for k in ("item_key", "citation_key", "citation", "similarity")}
                    for h in self.search_items(raw, k=candidates)
                ]
            results.append(entry)
        return results

    def list_items(self, filters: dict | None = None, sort: str = "year",
                   limit: int = LIST_LIMIT_MAX, offset: int = 0,
                   abstracts: bool = False) -> dict:
        """Every catalogue item a filter set admits, without a query: the
        total, and one page of short records in a fixed order ("year":
        newest first; "added": most recently added first; "citation":
        alphabetical; ties broken by citation, then item key, so pages
        never overlap). limit=0 returns the total only. abstracts=True adds
        each item's abstract (limit at most ABSTRACTS_MAX)."""
        if abstracts and limit > ABSTRACTS_MAX:
            raise ValueError(f"with abstracts, limit must be at most {ABSTRACTS_MAX};"
                             " page with offset")
        if sort not in ("year", "added", "citation"):
            raise ValueError("sort must be year, added or citation")
        if not 0 <= limit <= LIST_LIMIT_MAX:
            raise ValueError(f"limit must be 0-{LIST_LIMIT_MAX}; page with offset")
        if offset < 0:
            raise ValueError("offset must be 0 or more")
        flt = build_filter(filters) or FilterSQL("", [])
        if limit == 0:
            sql, params = flt.items_sql("COUNT(*)")
            total = self.store.query(sql, params)[0][0]
            return {"total": total, "offset": offset, "returned": 0,
                    "next_offset": None, "items": []}
        sql, params = flt.items_sql("i.payload")
        rows = []
        for r in self.store.query(sql, params):
            p = json.loads(r["payload"])
            rows.append({k: p.get(k) for k in _LIST_FIELDS} | {"_abstract": p.get("abstract", "")})

        def tie(r: dict) -> tuple:
            return ((r.get("citation") or "").lower(), r["item_key"])

        if sort == "year":
            rows.sort(key=tie)
            rows.sort(key=lambda r: (r.get("year_int") is None, -(r.get("year_int") or 0)))
        elif sort == "added":
            rows.sort(key=tie)
            rows.sort(key=lambda r: (r.get("added_ts") is None, -(r.get("added_ts") or 0)))
        else:
            rows.sort(key=tie)
        window = rows[offset : offset + limit]
        nxt = offset + len(window)
        return {
            "total": len(rows),
            "offset": offset,
            "returned": len(window),
            "next_offset": nxt if nxt < len(rows) else None,
            "items": [
                {**{k: r.get(k) for k in _LIST_FIELDS
                    if k not in ("year_int", "added_ts", "authors", "date_added")},
                 **_result_fields(r),
                 **({"abstract": _abstract({"abstract": r["_abstract"]})} if abstracts else {})}
                for r in window
            ],
        }
