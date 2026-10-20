"""Retrieval: dense search over the index.

Unfiltered searches match PaperQA2's default retrieval exactly: cosine
similarity top-k (PaperQA2 ships MMR but disables it by default via
mmr_lambda=1.0). Optional metadata filters (a scriptorium extension; PaperQA2
scopes retrieval by agentic paper selection instead) restrict the candidate
pool before similarity ranking. Relevance scoring of retrieved chunks
(PaperQA2's RCS step, an LLM task) is performed by Claude in-session per the
scriptorium skill — not here.
"""

import contextlib
import datetime
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from scriptorium.config import Config
from scriptorium.identifiers import classify
from scriptorium.index import CATALOGUE, COLLECTION, Embedder
from scriptorium.names import query_key

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
# Filter keys that full-text chunk points carry (the rest are catalogue-only).
CHUNK_FILTER_KEYS = {"creators", "creators_all", "years", "year_from", "year_to", "venue",
                     "citation_keys", "item_keys"}
ITEM_FILTER_KEYS = CHUNK_FILTER_KEYS | {"tags", "item_types", "collections", "has_pdf",
                                        "categories", "publishers", "venue_folders",
                                        "added_since"}
_LIST_FIELDS = ["item_key", "citation_key", "citation", "authors", "item_type", "year",
                "year_int", "venue_folders", "doi", "has_pdf", "date_added", "added_ts"]


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


def build_filter(filters: dict | None,
                 allowed: set[str] = ITEM_FILTER_KEYS) -> "qm.Filter | None":
    """Translate a filters dict into a Qdrant filter. A key outside
    `allowed` raises ValueError naming the valid keys, so a mistyped filter
    never widens a search without notice.

    Supported keys (all optional, AND-combined):
      creators:   list of surnames, any form ("van Atteveldt", "Atteveldt, W.";
                  case, accents and particles ignored; any match)
      creators_all: the same, but every listed author must be on the item
      years:      list of years (ints; any match)
      year_from / year_to: inclusive year range
      venue:      venue name (case-insensitive exact match)
      citation_keys / item_keys: explicit paper lists (any match)

    Catalogue-only keys (item-level search; chunk points lack these fields):
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
    must: list = []
    if v := filters.get("tags"):
        must.append(qm.FieldCondition(key="tags", match=qm.MatchAny(any=list(v))))
    if v := filters.get("item_types"):
        must.append(qm.FieldCondition(key="item_type", match=qm.MatchAny(any=list(v))))
    if v := filters.get("collections"):
        must.append(qm.FieldCondition(key="collections", match=qm.MatchAny(any=list(v))))
    if (v := filters.get("has_pdf")) is not None:
        must.append(qm.FieldCondition(key="has_pdf", match=qm.MatchValue(value=bool(v))))
    if v := filters.get("creators"):
        must.append(
            qm.FieldCondition(
                key="creator_surnames", match=qm.MatchAny(any=[query_key(s) for s in v])
            )
        )
    for name in filters.get("creators_all") or []:
        must.append(qm.FieldCondition(key="creator_surnames",
                                      match=qm.MatchValue(value=query_key(name))))
    if v := filters.get("years"):
        must.append(qm.FieldCondition(key="year_int", match=qm.MatchAny(any=[int(y) for y in v])))
    if filters.get("year_from") is not None or filters.get("year_to") is not None:
        must.append(
            qm.FieldCondition(
                key="year_int",
                range=qm.Range(gte=filters.get("year_from"), lte=filters.get("year_to")),
            )
        )
    if v := filters.get("venue"):
        must.append(qm.FieldCondition(key="venue_lc", match=qm.MatchValue(value=v.lower())))
    for name in ("categories", "publishers", "venue_folders"):
        if v := filters.get(name):
            must.append(qm.FieldCondition(key=f"{name}_lc", match=qm.MatchAny(any=[s.lower() for s in v])))
    if v := filters.get("added_since"):
        since = datetime.datetime.fromisoformat(v).replace(tzinfo=datetime.UTC)
        must.append(qm.FieldCondition(key="added_ts", range=qm.Range(gte=int(since.timestamp()))))
    if v := filters.get("citation_keys"):
        must.append(qm.FieldCondition(key="citation_key", match=qm.MatchAny(any=list(v))))
    if v := filters.get("item_keys"):
        must.append(qm.FieldCondition(key="item_key", match=qm.MatchAny(any=list(v))))
    return qm.Filter(must=must) if must else None


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
    """Query interface over the scriptorium index."""

    def __init__(
        self,
        config: Config,
        client: QdrantClient | None = None,
        client_provider: "Callable[[], QdrantClient] | None" = None,
        embedder: Embedder | None = None,
        client_lock: "threading.RLock | None" = None,
    ):
        self.config = config
        # A provider (e.g. the daemon's HubIndex.client property) is fetched
        # per call, so the store can be released and reopened between calls.
        # A fixed client is reused as-is; otherwise open our own.
        self._client_provider = client_provider
        self._client = client if client_provider is None else None
        self.embedder = embedder or Embedder(config.embedding_model, config.embedding_dim)
        self._client_lock = client_lock or contextlib.nullcontext()

    @property
    def client(self) -> QdrantClient:
        if self._client_provider is not None:
            return self._client_provider()
        if self._client is None:
            self._client = QdrantClient(path=str(self.config.index_dir / "qdrant"))
        return self._client

    def search(self, query: str, k: int = 10, filters: dict | None = None) -> list[SearchResult]:
        flt = build_filter(filters, CHUNK_FILTER_KEYS)
        query_vector = self.embedder.encode_query(query)
        with self._client_lock:
            if not self.client.collection_exists(COLLECTION):
                raise RuntimeError("Index is empty — run `scriptorium index` first.")
            hits = self.client.query_points(
                COLLECTION,
                query=query_vector,
                query_filter=flt,
                limit=k,
                with_payload=True,
            ).points
        return [
            SearchResult(
                rank=i,
                citation_key=h.payload["citation_key"] or h.payload["item_key"],
                citation=h.payload["citation"],
                name=h.payload["name"],
                text=h.payload["text"],
                similarity=float(h.score),
                item_key=h.payload["item_key"],
                chunk_index=h.payload["chunk_index"],
            )
            for i, h in enumerate(hits, start=1)
        ]

    def facets(self, field: str, contains: str = "", scope: str = "fulltext",
               filters: dict | None = None) -> dict:
        """Distinct values of a metadata field, with per-paper counts.
        scope "fulltext": across indexed chunks (the papers with full text;
        fields creators | venues | years). scope "items": across the whole
        catalogue (every item), which adds the fields categories |
        publishers | venue_folders | item_types. `filters` (build_filter's
        schema) counts within a filtered set; in scope "fulltext" only the
        chunk keys are allowed. Raises ValueError on an unknown scope,
        field or filter key."""
        keys = {"creators": "creator_surnames", "venues": "venue_lc", "years": "year"}
        if scope == "items":
            keys |= {"categories": "categories", "publishers": "publishers",
                     "venue_folders": "venue_folders", "item_types": "item_type"}
        elif scope != "fulltext":
            raise ValueError("scope must be fulltext or items")
        key = keys.get(field)
        if key is None:
            raise ValueError(f"field must be one of: {', '.join(keys)}")
        if scope == "fulltext" and (bad := set(filters or {}) - CHUNK_FILTER_KEYS):
            raise ValueError(f"filters {sorted(bad)} apply to scope 'items' only; scope"
                             f" 'fulltext' takes {sorted(CHUNK_FILTER_KEYS)}")
        collection = CATALOGUE if scope == "items" else COLLECTION
        counts: dict[str, set] = {}
        needle = (query_key(contains) or contains.lower()) if field == "creators" else contains.lower()
        with self._client_lock:
            offset = None
            while True:
                points, offset = self.client.scroll(
                    collection, limit=512,
                    scroll_filter=build_filter(
                        filters, ITEM_FILTER_KEYS if scope == "items" else CHUNK_FILTER_KEYS),
                    offset=offset, with_payload=[key, "item_key"], with_vectors=False,
                )
                for p in points:
                    values = p.payload.get(key)
                    values = values if isinstance(values, list) else [values]
                    for v in values:
                        if v and (not needle or needle in str(v).lower()):
                            counts.setdefault(str(v), set()).add(p.payload["item_key"])
                if offset is None:
                    break
        return {v: len(keys) for v, keys in sorted(counts.items())}

    def papers(self, filters: dict | None = None) -> list[dict]:
        """The papers a filter set admits (the retrieval pool): one record per
        paper with citation_key and short-form citation, sorted by key."""
        papers: dict[str, dict] = {}
        with self._client_lock:
            offset = None
            while True:
                points, offset = self.client.scroll(
                    COLLECTION,
                    scroll_filter=build_filter(filters, CHUNK_FILTER_KEYS),
                    limit=512,
                    offset=offset,
                    with_payload=["item_key", "citation_key", "citation"],
                    with_vectors=False,
                )
                for p in points:
                    papers.setdefault(
                        p.payload["item_key"],
                        {
                            "citation_key": p.payload["citation_key"] or p.payload["item_key"],
                            "citation": p.payload["citation"],
                        },
                    )
                if offset is None:
                    break
        return sorted(papers.values(), key=lambda r: r["citation_key"].lower())

    def search_items(
        self, query: str, k: int = 20, filters: dict | None = None, abstracts: bool = False
    ) -> list[dict]:
        """Item-level semantic search over the catalogue (the whole library,
        metadata embeddings). Returns one record per item, best first; with
        abstracts=True each carries its abstract (k at most ABSTRACTS_MAX)."""
        if abstracts and k > ABSTRACTS_MAX:
            raise ValueError(f"with abstracts, k must be at most {ABSTRACTS_MAX}")
        flt = build_filter(filters)
        query_vector = self.embedder.encode_item_query(query)
        with self._client_lock:
            if not self.client.collection_exists(CATALOGUE):
                raise RuntimeError("Catalogue is empty — run a sync first.")
            hits = self.client.query_points(
                CATALOGUE,
                query=query_vector,
                query_filter=flt,
                limit=k,
                with_payload=True,
            ).points
        return [
            {
                "rank": i,
                "similarity": round(float(h.score), 4),
                "item_key": h.payload["item_key"],
                "item_type": h.payload["item_type"],
                "citation_key": h.payload["citation_key"],
                "citation": h.payload["citation"],
                "year": h.payload["year"],
                "venue": h.payload.get("venue", ""),
                "venue_folders": h.payload.get("venue_folders", []),
                "doi": h.payload.get("doi", ""),
                "tags": h.payload["tags"],
                "has_pdf": h.payload["has_pdf"],
                **_result_fields(h.payload),
                **({"abstract": _abstract(h.payload)} if abstracts else {}),
            }
            for i, h in enumerate(hits, start=1)
        ]

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
                conditions = [qm.FieldCondition(key="title_norm", match=qm.MatchValue(value=value))]
            else:
                ident = f"{kind}:{value}"
                conditions = [
                    qm.FieldCondition(key=field, match=qm.MatchValue(value=ident))
                    for field in ("identifiers", "preprint_identifiers")
                ]
            with self._client_lock:
                if not self.client.collection_exists(CATALOGUE):
                    raise RuntimeError("Catalogue is empty — run a sync first.")
                points, _ = self.client.scroll(
                    CATALOGUE, scroll_filter=qm.Filter(should=conditions), limit=50,
                    with_payload=True, with_vectors=False,
                )
            matches = []
            for p in points:
                if kind == "title":
                    how = "title"
                elif f"{kind}:{value}" in p.payload.get("identifiers", []):
                    how = kind
                else:
                    how = "recorded preprint of this item"
                matches.append({
                    "item_key": p.payload["item_key"],
                    "citation_key": p.payload["citation_key"],
                    "citation": p.payload["citation"],
                    "item_type": p.payload["item_type"],
                    "doi": p.payload.get("doi", ""),
                    "has_pdf": p.payload["has_pdf"],
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
        flt = build_filter(filters)
        with self._client_lock:
            if not self.client.collection_exists(CATALOGUE):
                raise RuntimeError("Catalogue is empty — run a sync first.")
            if limit == 0:
                total = self.client.count(CATALOGUE, count_filter=flt, exact=True).count
                return {"total": total, "offset": offset, "returned": 0,
                        "next_offset": None, "items": []}
            rows, page = [], None
            while True:
                points, page = self.client.scroll(
                    CATALOGUE, scroll_filter=flt, limit=1024, offset=page,
                    with_payload=_LIST_FIELDS, with_vectors=False,
                )
                rows.extend({**p.payload, "_id": p.id} for p in points)
                if page is None:
                    break

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
        abstract_of = {}
        if abstracts and window:
            with self._client_lock:
                for p in self.client.retrieve(CATALOGUE, ids=[r["_id"] for r in window],
                                              with_payload=["abstract"]):
                    abstract_of[str(p.id)] = _abstract(p.payload)
        return {
            "total": len(rows),
            "offset": offset,
            "returned": len(window),
            "next_offset": nxt if nxt < len(rows) else None,
            "items": [
                {**{k: r.get(k) for k in _LIST_FIELDS
                    if k not in ("year_int", "added_ts", "authors", "date_added")},
                 **_result_fields(r),
                 **({"abstract": abstract_of.get(str(r["_id"]), "")} if abstracts else {})}
                for r in window
            ],
        }

