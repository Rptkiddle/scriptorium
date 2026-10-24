"""Embed chunks and catalogue entries and keep them in the persistent index.

Embedding: Qwen3-Embedding-0.6B via sentence-transformers on MPS. Documents
are encoded plain; queries use the model's instruction prompt (the asymmetric
scheme Qwen3 embeddings are trained for).

Storage: one SQLite file under the data directory (scriptorium.store),
holding the catalogue, the full-text chunks with their vectors, and the
sync state: the Zotero library-version watermark and the per-item
versions and hashes that make indexing incremental and idempotent. The
index records which model, revision and dimension made its vectors and
refuses an embedder that does not match.
"""

import datetime
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field

from scriptorium.config import Config
from scriptorium.identifiers import item_identifiers, normalise_title
from scriptorium.names import authors_from_formatted, keys_from_formatted
from scriptorium.store import STORE_FILE, VALUE_FIELDS, Store, dumps, to_blob
from scriptorium.tei import Passage
from scriptorium.zotero import PaperRecord

_QUERY_PROMPT = (
    "Instruct: Given a research question, retrieve relevant passages from"
    " academic papers that help answer the question\nQuery: "
)
_ITEM_QUERY_PROMPT = (
    "Instruct: Given a research topic or question, retrieve library items"
    " (papers, books, reports) relevant to it\nQuery: "
)


# The catalogue embeds what a paper is about; what it is (authors, year,
# venue, type, tags) is stored beside it and filtered exactly. Raising this
# number makes the next sync re-embed every catalogue entry.
CATALOGUE_DOC_VERSION = 2  # 1: title, citation, abstract, tags; 2: title, abstract
# Raising this number makes the next sync rewrite every catalogue entry's
# stored fields while keeping its vector.
CATALOGUE_PAYLOAD_VERSION = 2  # 1: identifiers, preprint_identifiers, title_norm; 2: authors
_FOLDER_MARKS = ("[✓] ", "[✗] ")
# The sync-state stamps kept in the store's meta table, with their types.
_STAMPS = {"library_version": int, "last_sync_at": str, "catalogue_meta": int,
           "catalogue_doc": int, "catalogue_payload": int, "catalogue_folders": str}


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
        self._revision: str | None = None
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
    def revision(self) -> str:
        """The model's revision (commit) in the local Hugging Face cache, or
        "" when it cannot be found. Read without loading the model."""
        if self._revision is None:
            try:
                from pathlib import Path

                from huggingface_hub import snapshot_download

                path = snapshot_download(self.model_name, local_files_only=True)
                self._revision = Path(path).name
            except Exception:  # noqa: BLE001 — not cached, or no hub library
                self._revision = ""
        return self._revision

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


def check_embedder(store: Store, embedder) -> None:
    """Refuse an embedder whose model revision differs from the one that
    made the stored vectors (vectors of two revisions are not comparable).
    The first vectors written record the revision. An embedder that cannot
    tell its revision (e.g. a test stub) is not checked."""
    revision = getattr(embedder, "revision", "") or ""
    if not revision:
        return
    stored = store.meta("embedding_revision")
    if not stored:
        store.set_meta("embedding_revision", revision)
    elif stored != revision:
        raise RuntimeError(
            f"The index was embedded with model revision {stored}, but the embedder"
            f" has revision {revision}. Vectors of different revisions cannot be"
            " compared: rebuild the index, or use the matching revision.")


@dataclass
class SyncState:
    """What the index believes about the library (for incremental sync).
    Kept in memory; HubIndex reads it from the store and writes back what
    changed (save_state)."""

    library_version: int
    items: dict[str, dict]  # item_key -> {version, content_hash, chunks, parser, meta}
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


def _creator_keys(record: PaperRecord) -> list[str]:
    """The author name keys the creators filter and facet match on
    (scriptorium.names); derived from the formatted creators string when the
    record carries no structured list."""
    return record.creator_keys or keys_from_formatted(record.creators)


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
        "added_ts": _added_ts(r.date_added),
        "identifiers": own,
        "preprint_identifiers": recorded,
        "title_norm": normalise_title(r.title),
        "authors": r.authors or authors_from_formatted(r.creators),
    }


_UPSERT_PAPER = (
    "INSERT INTO papers(item_key, version, content_hash, chunks, parser, meta)"
    " VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(item_key) DO UPDATE SET"
    " version = excluded.version, content_hash = excluded.content_hash,"
    " chunks = excluded.chunks, parser = excluded.parser, meta = excluded.meta")


class HubIndex:
    """The persistent index: the SQLite store plus the in-memory sync state.

    Every method that writes vectors does so in one transaction together
    with its bookkeeping; save_state() writes the rest of the state that
    changed in memory (the sync's own edits, e.g. failures and rebased
    versions)."""

    def __init__(self, config: Config, embedder=None, store: Store | None = None):
        self.config = config
        self.path = config.index_dir / STORE_FILE
        # Sibling files (the PDF hash cache) are placed beside this path.
        self.state_path = self.path
        self.embedder = embedder or Embedder(config.embedding_model, config.embedding_dim)
        self.store = store or Store(self.path)
        self._check_fingerprint()
        self._revision_checked = False
        self.state = self._load_state()
        self._saved = self._snapshot()

    # -- fingerprint and state ---------------------------------------------

    def _check_fingerprint(self) -> None:
        """Refuse to serve an index made by another model or dimension."""
        for key, want in (("embedding_model", self.config.embedding_model),
                          ("embedding_dim", str(self.config.embedding_dim))):
            have = self.store.meta(key)
            if not have:
                self.store.set_meta(key, want)
            elif have != want:
                raise RuntimeError(
                    f"The index at {self.path} was built with {key} {have}, but the"
                    f" configuration says {want}. Rebuild the index (move the file"
                    " aside and run a sync) or restore the configuration.")

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not self._revision_checked:
            check_embedder(self.store, self.embedder)
            self._revision_checked = True
        vectors = self.embedder.encode_documents(texts)
        if any(len(v) != self.config.embedding_dim for v in vectors):
            raise RuntimeError(f"the embedder returned vectors that are not"
                               f" {self.config.embedding_dim}-dimensional")
        return vectors

    def _load_state(self) -> SyncState:
        q = self.store.query
        stamps = {k: cast(self.store.meta(k, "0" if cast is int else ""))
                  for k, cast in _STAMPS.items()}
        failures = q("SELECT item_key, reason, attempt FROM failures")
        catalogue = q("SELECT item_key, version, has_pdf FROM items")
        return SyncState(
            items={r["item_key"]: {"version": r["version"], "content_hash": r["content_hash"],
                                   "chunks": r["chunks"], "parser": r["parser"],
                                   "meta": r["meta"]}
                   for r in q("SELECT * FROM papers")},
            failed={r["item_key"]: r["reason"] for r in failures},
            failed_hash={r["item_key"]: r["attempt"] for r in failures if r["attempt"]},
            catalogue={r["item_key"]: r["version"] for r in catalogue},
            catalogue_pdf={r["item_key"]: bool(r["has_pdf"]) for r in catalogue},
            **stamps,
        )

    def _snapshot(self) -> dict:
        s = self.state
        return {"items": {k: dict(v) for k, v in s.items.items()},
                "failed": dict(s.failed), "failed_hash": dict(s.failed_hash),
                "catalogue": dict(s.catalogue), "catalogue_pdf": dict(s.catalogue_pdf),
                "stamps": {k: getattr(s, k) for k in _STAMPS}}

    def save_state(self) -> None:
        """Write what changed in the in-memory state since the last save, in
        one transaction."""
        s, saved, changed = self.state, self._saved, set()
        with self.store.transaction() as st:
            for key in set(saved["items"]) | set(s.items):
                now = s.items.get(key)
                if now == saved["items"].get(key):
                    continue
                changed.add(key)
                if now is None:
                    st.execute("DELETE FROM papers WHERE item_key = ?", (key,))
                else:
                    st.execute(_UPSERT_PAPER, (
                        key, now.get("version", 0), now.get("content_hash", ""),
                        now.get("chunks", 0), now.get("parser"), now.get("meta")))
            failure_keys = (set(saved["failed"]) | set(s.failed)
                            | set(saved["failed_hash"]) | set(s.failed_hash))
            for key in failure_keys:
                now = (s.failed.get(key), s.failed_hash.get(key, ""))
                if now == (saved["failed"].get(key), saved["failed_hash"].get(key, "")):
                    continue
                changed.add(key)
                if now[0] is None:
                    st.execute("DELETE FROM failures WHERE item_key = ?", (key,))
                else:
                    st.execute("INSERT INTO failures(item_key, reason, attempt) VALUES(?, ?, ?)"
                               " ON CONFLICT(item_key) DO UPDATE SET reason = excluded.reason,"
                               " attempt = excluded.attempt", (key, *now))
            for key in set(s.catalogue) | set(s.catalogue_pdf):
                now = (s.catalogue.get(key), s.catalogue_pdf.get(key))
                if now != (saved["catalogue"].get(key), saved["catalogue_pdf"].get(key)):
                    changed.add(key)
                    st.execute("UPDATE items SET version = ?, has_pdf = ? WHERE item_key = ?",
                               (now[0] or 0, int(bool(now[1])), key))
            for k in _STAMPS:
                if getattr(s, k) != saved["stamps"][k]:
                    st.set_meta(k, getattr(s, k))
        self._mark_saved(*changed)
        self._saved["stamps"] = {k: getattr(s, k) for k in _STAMPS}

    def _mark_saved(self, *keys: str) -> None:
        """A method wrote these keys' state itself: record the current values
        as saved, so save_state does not write them again."""
        for key in keys:
            for part in ("items", "failed", "failed_hash", "catalogue", "catalogue_pdf"):
                current = getattr(self.state, part)
                if key in current:
                    value = current[key]
                    self._saved[part][key] = dict(value) if isinstance(value, dict) else value
                else:
                    self._saved[part].pop(key, None)

    # -- full text -----------------------------------------------------------

    def classify_change(self, record: PaperRecord, content_hash: str) -> str:
        """What (re)indexing an item needs: 'embed', 'metadata', or 'none'.

        The PDF's content hash and the parser version decide whether chunks
        and vectors must be rebuilt; a Zotero version bump alone (a metadata
        edit) or a METADATA_VERSION change needs only the bookkeeping updated
        (chunks read their metadata from the item's catalogue row).
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
        """Record a metadata-only change. Chunks carry no metadata of their
        own (citation, creators, year and venue are read from the item's
        catalogue row, which the sync's catalogue pass has rewritten), so
        only the bookkeeping changes. Returns the paper's chunk count."""
        from scriptorium.zotero import METADATA_VERSION

        known = self.state.items.get(record.item_key)
        if known is None:
            return 0
        known["version"] = record.version
        known["meta"] = METADATA_VERSION
        return known.get("chunks", 0)

    def delete_paper(self, item_key: str) -> None:
        with self.store.transaction() as st:
            st.execute("DELETE FROM chunks WHERE item_key = ?", (item_key,))
            st.execute("DELETE FROM papers WHERE item_key = ?", (item_key,))
            st.execute("DELETE FROM failures WHERE item_key = ?", (item_key,))
        self.state.items.pop(item_key, None)
        self.state.failed.pop(item_key, None)
        self.state.failed_hash.pop(item_key, None)
        self._mark_saved(item_key)
        self.store.generation["chunks"] += 1

    def add_paper(self, record: PaperRecord, chunks: list[Passage], content_hash: str) -> int:
        """Embed one paper's passages and store them, replacing any earlier
        ones, in one transaction with its bookkeeping. Artefacts are stored
        without a vector. Returns the count."""
        from scriptorium.parsing import PARSER_VERSION
        from scriptorium.zotero import METADATA_VERSION

        embedded = iter(self._encode([c.embed_text for c in chunks if not c.artefact]))
        rows = [(record.item_key, i, c.part, c.pages, c.text, int(c.artefact),
                 None if c.artefact else to_blob(next(embedded)))
                for i, c in enumerate(chunks)]
        entry = {"version": record.version, "content_hash": content_hash,
                 "chunks": len(rows), "parser": PARSER_VERSION, "meta": METADATA_VERSION}
        with self.store.transaction() as st:
            st.execute("DELETE FROM chunks WHERE item_key = ?", (record.item_key,))
            st.conn.executemany(
                "INSERT INTO chunks(item_key, chunk_index, part, pages, text, artefact, vec)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                rows)
            st.execute(_UPSERT_PAPER, (record.item_key, *entry.values()))
            st.execute("DELETE FROM failures WHERE item_key = ?", (record.item_key,))
        self.state.items[record.item_key] = entry
        self.state.failed.pop(record.item_key, None)
        self.state.failed_hash.pop(record.item_key, None)
        self._mark_saved(record.item_key)
        self.store.generation["chunks"] += 1
        return len(rows)

    def record_failure(self, item_key: str, reason: str, attempt: str = "") -> None:
        """attempt: "content_hash:parser" of what failed ("" if unknown), so
        a sync can skip re-parsing until the PDF or the parser changes."""
        self.state.failed[item_key] = reason
        if attempt:
            self.state.failed_hash[item_key] = attempt
        else:
            self.state.failed_hash.pop(item_key, None)

    # -- catalogue (item-level metadata index, whole library) ----------------

    @staticmethod
    def _write_fields(st: Store, fields: dict, vec: bytes | None, version: int) -> bool:
        """Write one item's stored fields (and its vector, when given).
        Without a vector only an existing row is rewritten (its has_pdf
        column too, so filters agree with the stored fields at once);
        returns whether a row was written."""
        columns = (fields["citation_key"], fields["item_type"], fields["year_int"],
                   fields["venue_lc"], fields["added_ts"], fields["title_norm"], dumps(fields))
        key = fields["item_key"]
        if vec is None:
            cur = st.execute(
                "UPDATE items SET citation_key = ?, item_type = ?, year_int = ?, venue_lc = ?,"
                " added_ts = ?, title_norm = ?, payload = ?, has_pdf = ? WHERE item_key = ?",
                (*columns, int(fields["has_pdf"]), key))
            if cur.rowcount == 0:
                return False
        else:
            st.execute(
                "INSERT INTO items(item_key, version, has_pdf, citation_key, item_type, year_int,"
                " venue_lc, added_ts, title_norm, payload, vec)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(item_key) DO UPDATE SET version = excluded.version,"
                " has_pdf = excluded.has_pdf, citation_key = excluded.citation_key,"
                " item_type = excluded.item_type, year_int = excluded.year_int,"
                " venue_lc = excluded.venue_lc, added_ts = excluded.added_ts,"
                " title_norm = excluded.title_norm, payload = excluded.payload, vec = excluded.vec",
                (key, version, int(fields["has_pdf"]), *columns, vec))
        st.execute("DELETE FROM item_values WHERE item_key = ?", (key,))
        st.conn.executemany(
            "INSERT INTO item_values(item_key, field, value, value_lc) VALUES(?, ?, ?, ?)",
            [(key, name, str(v), str(v).lower())
             for name, source in VALUE_FIELDS.items() for v in fields.get(source) or []])
        return True

    def upsert_catalogue(self, records: list["PaperRecord"],
                         folders: dict[str, tuple[str, str, str]] | None = None) -> int:
        """Embed and store catalogue entries for these items (one batch, one
        transaction). `folders` (from folder_paths) names each item's
        category, publisher and venue folders."""
        if not records:
            return 0
        folders = folders or {}
        vectors = self._encode([catalogue_document(r) for r in records])
        with self.store.transaction() as st:
            for r, v in zip(records, vectors, strict=True):
                self._write_fields(st, catalogue_fields(r, folders), to_blob(v), r.version)
        for r in records:
            self.state.catalogue[r.item_key] = r.version
            self.state.catalogue_pdf[r.item_key] = r.pdf_status == "ok"
        self._mark_saved(*(r.item_key for r in records))
        self.store.generation["items"] += 1
        return len(records)

    def refresh_catalogue_payloads(self, records: list["PaperRecord"],
                                   folders: dict[str, tuple[str, str, str]] | None = None) -> int:
        """Rewrite these items' stored fields, keeping each vector (no
        embedding). Items not yet in the catalogue are skipped. Returns the
        number rewritten."""
        if not records:
            return 0
        folders = folders or {}
        with self.store.transaction() as st:
            return sum(self._write_fields(st, catalogue_fields(r, folders), None, r.version)
                       for r in records)

    def delete_catalogue(self, item_key: str) -> None:
        self.store.execute("DELETE FROM items WHERE item_key = ?", (item_key,))
        self.state.catalogue.pop(item_key, None)
        self.state.catalogue_pdf.pop(item_key, None)
        self._mark_saved(item_key)
        self.store.generation["items"] += 1

    def catalogue_lookup(self, key: str) -> dict | None:
        """One catalogue entry's stored fields by item key or citation key, or None."""
        rows = self.store.query("SELECT payload FROM items WHERE item_key = ? OR citation_key = ?"
                                " ORDER BY item_key = ? DESC LIMIT 1", (key, key, key))
        return json.loads(rows[0]["payload"]) if rows else None

    def counts(self) -> dict:
        chunks = self.store.query("SELECT COUNT(*) AS n FROM chunks")[0]["n"]
        return {
            "papers": len(self.state.items),
            "chunks": chunks,
            "catalogue_items": len(self.state.catalogue),
            "failed": len(self.state.failed),
            "library_version": self.state.library_version,
        }

    def close(self) -> None:
        self.store.close()
