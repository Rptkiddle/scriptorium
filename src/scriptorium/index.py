"""Embed chunks and store them in the persistent index.

Embedding: Qwen3-Embedding-0.6B via sentence-transformers on MPS. Documents
are encoded plain; queries use the model's instruction prompt (the asymmetric
scheme Qwen3 embeddings are trained for).

Storage: embedded Qdrant (on-disk, single-process) under the data directory,
plus a JSON sync-state file recording the Zotero library-version watermark
and per-item versions/hashes that make indexing incremental and idempotent.
"""

import contextlib
import datetime
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from qdrant_client import QdrantClient, models

from scriptorium.config import Config
from scriptorium.identifiers import item_identifiers, normalise_title
from scriptorium.names import authors_from_formatted, keys_from_formatted
from scriptorium.zotero import PaperRecord

COLLECTION = "scriptorium"
CATALOGUE = "scriptorium_catalogue"
_QUERY_PROMPT = (
    "Instruct: Given a research question, retrieve relevant passages from"
    " academic papers that help answer the question\nQuery: "
)
_ITEM_QUERY_PROMPT = (
    "Instruct: Given a research topic or question, retrieve library items"
    " (papers, books, reports) relevant to it\nQuery: "
)


# The catalogue embeds what a paper is about; what it is (authors, year,
# venue, type, tags) is payload, filtered exactly. Raising this number makes
# the next sync re-embed every catalogue entry.
CATALOGUE_DOC_VERSION = 2  # 1: title, citation, abstract, tags; 2: title, abstract
# Raising this number makes the next sync rewrite every catalogue entry's
# stored fields (payload) while keeping its vector.
CATALOGUE_PAYLOAD_VERSION = 2  # 1: identifiers, preprint_identifiers, title_norm; 2: authors
_FOLDER_MARKS = ("[✓] ", "[✗] ")


def catalogue_document(record: "PaperRecord") -> str:
    """The text embedded for item-level search: title and abstract."""
    parts = [record.title]
    if record.abstract:
        parts.append(" ".join(record.abstract.split()))
    return "\n".join(p for p in parts if p)


def folder_paths(collections: list[dict]) -> dict[str, tuple[str, str, str]]:
    """Collection key -> (category, publisher, venue): the folder names on
    its path down from the top level (category > publisher > venue), with
    the subscription marks removed; a shallower folder leaves the later
    names empty."""
    by_key = {c["key"]: c for c in collections}
    paths = {}
    for c in collections:
        names, node, seen = [], c, set()
        while node is not None and node["key"] not in seen:
            seen.add(node["key"])
            names.append(node["name"])
            node = by_key.get(node.get("parent") or "")
        names = [n.removeprefix(_FOLDER_MARKS[0]).removeprefix(_FOLDER_MARKS[1])
                 for n in reversed(names)]
        paths[c["key"]] = tuple((names + ["", "", ""])[:3])
    return paths


def folders_signature(folders: dict[str, tuple[str, str, str]]) -> str:
    """A short fingerprint of every folder's category > publisher > venue
    names, so a sync notices renamed or moved folders."""
    text = json.dumps(sorted(folders.items()), ensure_ascii=False)
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _added_ts(date_added: str) -> int | None:
    """Zotero's dateAdded ("2026-10-15T18:36:50Z") as epoch seconds."""
    try:
        return int(datetime.datetime.fromisoformat(date_added).timestamp())
    except ValueError:
        return None


class Embedder:
    """Lazy-loading sentence-transformers wrapper (document/query modes).

    The model loads on first use and can be unloaded when idle (the daemon's
    reaper thread calls unload(); the next encode reloads transparently).
    """

    def __init__(self, model_name: str, truncate_dim: int | None = None):
        self.model_name = model_name
        self.truncate_dim = truncate_dim
        self._model = None
        self.last_used: float = 0.0
        # Model inference on MPS is not thread-safe; all encode calls are
        # serialized. Documents encode in sub-batches so a concurrent query
        # embed waits at most one sub-batch, not a whole paper.
        self._lock = threading.Lock()

    @property
    def model(self):
        if self._model is None:
            import os

            # Load from the local HF cache without network checks: the hub
            # freshness ping can hang indefinitely on flaky connections.
            # Set SCRIPTORIUM_HF_ONLINE=1 to allow a first-time model download.
            if os.environ.get("SCRIPTORIUM_HF_ONLINE", "") not in {"1", "true"}:
                os.environ.setdefault("HF_HUB_OFFLINE", "1")

            import torch
            from sentence_transformers import SentenceTransformer

            device = "mps" if torch.backends.mps.is_available() else "cpu"
            try:
                # truncate_dim relies on the model's Matryoshka training:
                # vectors are cut to the leading dimensions, then normalised.
                self._model = SentenceTransformer(
                    self.model_name, device=device, truncate_dim=self.truncate_dim
                )
            except OSError as exc:
                raise RuntimeError(
                    f"Embedding model {self.model_name!r} not found in the local"
                    " HF cache. Run once with SCRIPTORIUM_HF_ONLINE=1 to download it."
                ) from exc
        return self._model

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def unload(self) -> None:
        """Free the model's memory; the next encode reloads it."""
        import gc

        with self._lock:
            if self._model is None:
                return
            self._model = None
            gc.collect()
            import torch

            if torch.backends.mps.is_available():
                torch.mps.empty_cache()

    def encode_documents(self, texts: list[str], batch_size: int = 16) -> list[list[float]]:
        self.last_used = time.monotonic()
        out: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            with self._lock:
                vectors = self.model.encode(
                    texts[i : i + batch_size],
                    batch_size=batch_size,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
            out.extend(v.tolist() for v in vectors)
        # Release the MPS allocator's cached buffers after each document —
        # without this a full-library sync accumulates tens of GB of cache.
        import torch

        if torch.backends.mps.is_available():
            with self._lock:
                torch.mps.empty_cache()
        return out

    def encode_query(self, text: str) -> list[float]:
        self.last_used = time.monotonic()
        with self._lock:
            vector = self.model.encode(
                [_QUERY_PROMPT + text], normalize_embeddings=True
            )[0]
        return vector.tolist()

    def encode_item_query(self, text: str) -> list[float]:
        self.last_used = time.monotonic()
        with self._lock:
            vector = self.model.encode(
                [_ITEM_QUERY_PROMPT + text], normalize_embeddings=True
            )[0]
        return vector.tolist()


@dataclass
class SyncState:
    """What the index believes about the library (for incremental sync)."""

    library_version: int
    items: dict[str, dict]  # item_key -> {version, content_hash, chunks}
    failed: dict[str, str]  # item_key -> failure reason
    catalogue: dict[str, int]  # item_key -> version (item-level metadata index)
    catalogue_meta: int = 0  # METADATA_VERSION the whole catalogue was built with
    catalogue_doc: int = 0  # CATALOGUE_DOC_VERSION the whole catalogue was embedded with
    catalogue_payload: int = 0  # CATALOGUE_PAYLOAD_VERSION of the stored fields
    catalogue_folders: str = ""  # folders_signature() the stored folder names match
    last_sync_at: str = ""  # UTC ISO start time of the last completed sync
    # item_key -> has_pdf as last catalogued; a mismatch re-catalogues the
    # item (attaching a PDF need not bump the parent's Zotero version)
    catalogue_pdf: dict[str, bool] = field(default_factory=dict)
    # item_key -> "content_hash:parser" of the attempt that failed; a sync
    # retries the item only when either changes
    failed_hash: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        if path.exists():
            raw = json.loads(path.read_text())
            return cls(
                library_version=raw.get("library_version", 0),
                items=raw.get("items", {}),
                failed=raw.get("failed", {}),
                catalogue=raw.get("catalogue", {}),
                catalogue_meta=raw.get("catalogue_meta", 0),
                catalogue_doc=raw.get("catalogue_doc", 0),
                catalogue_payload=raw.get("catalogue_payload", 0),
                catalogue_folders=raw.get("catalogue_folders", ""),
                last_sync_at=raw.get("last_sync_at", ""),
                catalogue_pdf=raw.get("catalogue_pdf", {}),
                failed_hash=raw.get("failed_hash", {}),
            )
        return cls(library_version=0, items={}, failed={}, catalogue={})

    def save(self, path: Path) -> None:
        """Atomic: write a temp file beside the target, then rename over it,
        so a crash mid-write never leaves a truncated state file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "library_version": self.library_version,
                    "items": self.items,
                    "failed": self.failed,
                    "catalogue": self.catalogue,
                    "catalogue_meta": self.catalogue_meta,
                    "catalogue_doc": self.catalogue_doc,
                    "catalogue_payload": self.catalogue_payload,
                    "catalogue_folders": self.catalogue_folders,
                    "last_sync_at": self.last_sync_at,
                    "catalogue_pdf": self.catalogue_pdf,
                    "failed_hash": self.failed_hash,
                },
                indent=1,
            )
        )
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def _point_id(item_key: str, chunk_index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"scriptorium:{item_key}:{chunk_index}"))


def _creator_keys(record: PaperRecord) -> list[str]:
    """The author name keys the creators filter and facet match on
    (scriptorium.names); derived from the formatted creators string when the
    record carries no structured list."""
    return record.creator_keys or keys_from_formatted(record.creators)


def _catalogue_id(item_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"scriptorium:item:{item_key}"))


def catalogue_fields(r: PaperRecord, folders: dict[str, tuple[str, str, str]]) -> dict:
    """The stored fields of one catalogue entry: what an item is (filters,
    identifiers, result fields), beside the vector of what it is about."""

    def names(level: int) -> list[str]:
        return sorted({folders[k][level] for k in r.collections
                       if k in folders and folders[k][level]})

    own, recorded = item_identifiers(r.doi, r.archive_id, r.extra)
    return {
        "item_key": r.item_key,
        "item_type": r.item_type,
        "citation_key": r.citation_key,
        "citation": r.citation,
        "title": r.title,
        "creators": r.creators,
        "year": r.year,
        "doi": r.doi,
        "venue": r.venue,
        "abstract": r.abstract,
        "tags": r.tags,
        "collections": r.collections,
        "date_added": r.date_added,
        "has_pdf": r.pdf_status == "ok",
        "creator_surnames": _creator_keys(r),
        "year_int": int(r.year) if r.year.isdigit() else None,
        "venue_lc": r.venue.lower(),
        "categories": names(0),
        "publishers": names(1),
        "venue_folders": names(2),
        "categories_lc": [n.lower() for n in names(0)],
        "publishers_lc": [n.lower() for n in names(1)],
        "venue_folders_lc": [n.lower() for n in names(2)],
        "added_ts": _added_ts(r.date_added),
        "identifiers": own,
        "preprint_identifiers": recorded,
        "title_norm": normalise_title(r.title),
        "authors": r.authors or authors_from_formatted(r.creators),
    }


class HubIndex:
    """The persistent chunk index (embedded Qdrant + sync state)."""

    def __init__(
        self,
        config: Config,
        client: QdrantClient | None = None,
        embedder: Embedder | None = None,
        client_lock: "threading.RLock | None" = None,
    ):
        self.config = config
        self.qdrant_path = config.index_dir / "qdrant"
        self.state_path = config.index_dir / "state.json"
        self.embedder = embedder or Embedder(config.embedding_model, config.embedding_dim)
        self.qdrant_path.parent.mkdir(parents=True, exist_ok=True)
        # The daemon shares one client across threads; guard its calls.
        self._client_lock = client_lock or contextlib.nullcontext()
        # The store opens lazily and can be released when idle (the daemon's
        # reaper thread calls release_client(); the next access reopens it).
        self._client = client
        self._external_client = client is not None
        self.client_last_used: float = 0.0
        self._cached_chunk_count = 0
        self.state = SyncState.load(self.state_path)
        self._check_dim()

    @property
    def client(self) -> QdrantClient:
        with self._client_lock:
            if self._client is None:
                self._client = QdrantClient(path=str(self.qdrant_path))
            self.client_last_used = time.monotonic()
            return self._client

    def release_client(self) -> None:
        """Close the embedded store; the next access reopens it."""
        with self._client_lock:
            if self._client is None or self._external_client:
                return
            self._client.close()
            self._client = None

    @property
    def store_open(self) -> bool:
        return self._client is not None

    def _check_dim(self) -> None:
        """Refuse to serve an index whose vector size doesn't match config."""
        for name in (COLLECTION, CATALOGUE):
            with self._client_lock:
                if not self.client.collection_exists(name):
                    continue
                stored = self.client.get_collection(name).config.params.vectors.size
            if stored != self.config.embedding_dim:
                raise RuntimeError(
                    f"Collection {name} vector size is {stored} but"
                    f" SCRIPTORIUM_EMBEDDING_DIM is {self.config.embedding_dim}."
                    " Run scripts/migrate_embedding_dim.py (or rebuild the index)"
                    " before serving."
                )

    def _ensure_collection(self, name: str = COLLECTION) -> None:
        with self._client_lock:
            if not self.client.collection_exists(name):
                self.client.create_collection(
                    name,
                    vectors_config=models.VectorParams(
                        size=self.config.embedding_dim, distance=models.Distance.COSINE
                    ),
                )

    def classify_change(self, record: PaperRecord, content_hash: str) -> str:
        """What (re)indexing an item needs: 'embed', 'metadata', or 'none'.

        The PDF's content hash and the parser version decide whether chunks
        and vectors must be rebuilt; a Zotero version bump alone (a metadata
        edit) or a METADATA_VERSION change only requires rewriting payloads
        in place.
        """
        from scriptorium.parsing import PARSER_VERSION
        from scriptorium.zotero import METADATA_VERSION

        known = self.state.items.get(record.item_key)
        if known is None:
            return "embed"
        if known["content_hash"] != content_hash or known.get("parser") != PARSER_VERSION:
            return "embed"
        if known["version"] != record.version or known.get("meta") != METADATA_VERSION:
            return "metadata"
        return "none"

    def update_metadata(self, record: PaperRecord) -> int:
        """Refresh an item's payloads from Zotero metadata without re-embedding.

        Rewrites the citation/filter fields on every point in one call per
        item, and a chunk's name only when its citation-key prefix changed.
        Returns the point count.
        """
        docname = record.citation_key or record.item_key
        common = {
            "citation_key": record.citation_key,
            "citation": record.citation,
            "title": record.title,
            "creators": record.creators,
            "year": record.year,
            "doi": record.doi,
            "venue": record.venue,
            "creator_surnames": _creator_keys(record),
            "year_int": int(record.year) if record.year.isdigit() else None,
            "venue_lc": record.venue.lower(),
        }
        item = models.Filter(must=[
            models.FieldCondition(key="item_key", match=models.MatchValue(value=record.item_key))
        ])
        updated = 0
        with self._client_lock:
            self.client.set_payload(COLLECTION, payload=common,
                                    points=models.FilterSelector(filter=item))
            offset = None
            while True:
                points, offset = self.client.scroll(
                    COLLECTION, scroll_filter=item, limit=256, offset=offset,
                    with_payload=["name"], with_vectors=False,
                )
                for p in points:
                    updated += 1
                    prefix, sep, suffix = p.payload.get("name", "").partition(" pages ")
                    if sep and prefix != docname:
                        self.client.set_payload(COLLECTION, points=[p.id],
                                                payload={"name": f"{docname} pages {suffix}"})
                if offset is None:
                    break
        from scriptorium.zotero import METADATA_VERSION

        if record.item_key in self.state.items:
            self.state.items[record.item_key]["version"] = record.version
            self.state.items[record.item_key]["meta"] = METADATA_VERSION
        return updated

    def delete_paper(self, item_key: str) -> None:
        with self._client_lock:
            if self.client.collection_exists(COLLECTION):
                self.client.delete(
                    COLLECTION,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
                            must=[
                                models.FieldCondition(
                                    key="item_key", match=models.MatchValue(value=item_key)
                                )
                            ]
                        )
                    ),
                )
        self.state.items.pop(item_key, None)
        self.state.failed.pop(item_key, None)
        self.state.failed_hash.pop(item_key, None)

    def add_paper(self, record: PaperRecord, chunks, content_hash: str) -> int:
        """Embed and upsert one paper's chunks. Returns the chunk count."""
        self._ensure_collection()
        self.delete_paper(record.item_key)  # idempotent re-ingest
        texts = [c.text for c in chunks]
        vectors = self.embedder.encode_documents(texts)
        points = [
            models.PointStruct(
                id=_point_id(record.item_key, i),
                vector=vec,
                payload={
                    "item_key": record.item_key,
                    "chunk_index": i,
                    "name": chunk.name,
                    "text": chunk.text,
                    "citation_key": record.citation_key,
                    "citation": record.citation,
                    "title": record.title,
                    "creators": record.creators,
                    "year": record.year,
                    "doi": record.doi,
                    "venue": record.venue,
                    # filter-ready fields (see HubSearch filters)
                    "creator_surnames": _creator_keys(record),
                    "year_int": int(record.year) if record.year.isdigit() else None,
                    "venue_lc": record.venue.lower(),
                },
            )
            for i, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True))
        ]
        with self._client_lock:
            self.client.upsert(COLLECTION, points=points)
        from scriptorium.parsing import PARSER_VERSION
        from scriptorium.zotero import METADATA_VERSION

        self.state.items[record.item_key] = {
            "version": record.version,
            "content_hash": content_hash,
            "chunks": len(points),
            "parser": PARSER_VERSION,
            "meta": METADATA_VERSION,
        }
        return len(points)

    def record_failure(self, item_key: str, reason: str, attempt: str = "") -> None:
        """attempt: "content_hash:parser" of what failed ("" if unknown), so
        a sync can skip re-parsing until the PDF or the parser changes."""
        self.state.failed[item_key] = reason
        if attempt:
            self.state.failed_hash[item_key] = attempt
        else:
            self.state.failed_hash.pop(item_key, None)

    # -- catalogue (item-level metadata index, whole library) ----------------

    def upsert_catalogue(self, records: list["PaperRecord"],
                         folders: dict[str, tuple[str, str, str]] | None = None) -> int:
        """Embed and upsert catalogue points for these items (batched).
        `folders` (from folder_paths) names each item's category, publisher
        and venue folders in the payload."""
        if not records:
            return 0
        folders = folders or {}
        self._ensure_collection(CATALOGUE)
        vectors = self.embedder.encode_documents([catalogue_document(r) for r in records])
        points = [
            models.PointStruct(id=_catalogue_id(r.item_key), vector=vec,
                               payload=catalogue_fields(r, folders))
            for r, vec in zip(records, vectors, strict=True)
        ]
        with self._client_lock:
            self.client.upsert(CATALOGUE, points=points)
        for r in records:
            self.state.catalogue[r.item_key] = r.version
            self.state.catalogue_pdf[r.item_key] = r.pdf_status == "ok"
        return len(points)

    def refresh_catalogue_payloads(self, records: list["PaperRecord"],
                                   folders: dict[str, tuple[str, str, str]] | None = None) -> int:
        """Rewrite these items' catalogue payloads, keeping each stored
        vector (no embedding). Items not yet in the catalogue are skipped.
        Returns the number rewritten."""
        if not records:
            return 0
        folders = folders or {}
        by_id = {_catalogue_id(r.item_key): r for r in records}
        with self._client_lock:
            if not self.client.collection_exists(CATALOGUE):
                return 0
            stored = self.client.retrieve(CATALOGUE, ids=list(by_id), with_vectors=True,
                                          with_payload=False)
            points = [
                models.PointStruct(id=p.id, vector=p.vector,
                                   payload=catalogue_fields(by_id[str(p.id)], folders))
                for p in stored
            ]
            if points:
                self.client.upsert(CATALOGUE, points=points)
        return len(points)

    def delete_catalogue(self, item_key: str) -> None:
        self.state.catalogue_pdf.pop(item_key, None)
        with self._client_lock:
            if self.client.collection_exists(CATALOGUE):
                self.client.delete(
                    CATALOGUE,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
                            must=[
                                models.FieldCondition(
                                    key="item_key", match=models.MatchValue(value=item_key)
                                )
                            ]
                        )
                    ),
                )
        self.state.catalogue.pop(item_key, None)

    def catalogue_lookup(self, key: str) -> dict | None:
        """One catalogue payload by item key or citation key, or None."""
        with self._client_lock:
            if not self.client.collection_exists(CATALOGUE):
                return None
            points, _ = self.client.scroll(
                CATALOGUE,
                scroll_filter=models.Filter(
                    should=[
                        models.FieldCondition(key="item_key", match=models.MatchValue(value=key)),
                        models.FieldCondition(key="citation_key", match=models.MatchValue(value=key)),
                    ]
                ),
                limit=1,
                with_payload=True,
            )
        return points[0].payload if points else None

    def counts(self) -> dict:
        # Frequent health polls must not hold the store open: while it is
        # released, serve the last computed chunk count instead of reopening.
        with self._client_lock:
            if self._client is not None:
                self._cached_chunk_count = (
                    self._client.count(COLLECTION).count
                    if self._client.collection_exists(COLLECTION)
                    else 0
                )
        return {
            "papers": len(self.state.items),
            "chunks": self._cached_chunk_count,
            "catalogue_items": len(self.state.catalogue),
            "failed": len(self.state.failed),
            "library_version": self.state.library_version,
        }

    def save_state(self) -> None:
        self.state.save(self.state_path)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
