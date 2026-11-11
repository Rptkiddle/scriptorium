"""Embedding of item texts (title and abstract) and passages, and the two
functions that fill the vectors still missing in the database.

Documents are encoded plain; queries carry the model's instruction prompt
(the asymmetric scheme the model is trained for). Model and dimension are
fixed here because every stored vector depends on them: changing either
means embedding the whole collection again.
"""

import hashlib
import math
import re
import threading
import time

import psycopg

MODEL = "Qwen/Qwen3-Embedding-0.6B"
# Matryoshka truncation: vectors are cut to this many dimensions before
# normalisation.
DIM = 256

_QUERY_PROMPT = (
    "Instruct: Given a research question, retrieve relevant passages from"
    " academic papers that help answer the question\nQuery: "
)
_ITEM_QUERY_PROMPT = (
    "Instruct: Given a research topic or question, retrieve library items"
    " (papers, books, reports) relevant to it\nQuery: "
)


class Embedder:
    """What the rest of the server asks of an embedder: vectors of DIM
    numbers, unit length, for documents and for queries."""

    revision = ""  # the model's revision; "" when the embedder has none

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per document text (an item's title and abstract, or a passage)."""
        raise NotImplementedError

    def embed_query(self, text: str, kind: str = "passages") -> list[float]:
        """The vector of a search query, for searching "passages" or "items"."""
        raise NotImplementedError


class FakeEmbedder(Embedder):
    """A deterministic stand-in for tests: each word adds weight to one of
    the DIM dimensions, so texts sharing words have nearby vectors. No model."""

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * DIM
        for word in re.findall(r"\w+", text.lower()):
            vector[int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        if norm == 1.0 and not any(vector):
            vector[0] = 1.0
        return [x / norm for x in vector]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str, kind: str = "passages") -> list[float]:
        return self._vector(text)


class RealEmbedder(Embedder):
    """The embedding model, through sentence-transformers, on the GPU when
    there is one.

    The model loads on first use and can be unloaded when idle (unload();
    the next encode reloads it).
    """

    def __init__(self, model_name: str = MODEL, truncate_dim: int | None = DIM):
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

            if torch.cuda.is_available():
                device = "cuda"
            elif torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
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
    def revision(self) -> str:  # noqa: F811 (replaces the base's empty value)
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

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            elif torch.backends.mps.is_available():
                torch.mps.empty_cache()

    def embed(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
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
        # Release the GPU allocator's cached buffers after each document —
        # without this a full-library sync accumulates tens of GB of cache.
        import torch

        if torch.cuda.is_available():
            with self._lock:
                torch.cuda.empty_cache()
        elif torch.backends.mps.is_available():
            with self._lock:
                torch.mps.empty_cache()
        return out

    def embed_query(self, text: str, kind: str = "passages") -> list[float]:
        prompt = {"passages": _QUERY_PROMPT, "items": _ITEM_QUERY_PROMPT}[kind]
        self.last_used = time.monotonic()
        with self._lock:
            vector = self.model.encode([prompt + text], normalize_embeddings=True)[0]
        return vector.tolist()


def item_document(title: str, abstract: str | None) -> str:
    """The text embedded for an item: its title and abstract."""
    parts = [title, " ".join((abstract or "").split())]
    return "\n".join(p for p in parts if p)


def vector_literal(vector: list[float]) -> str:
    """A vector as PostgreSQL reads it."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def check_revision(conn: psycopg.Connection, embedder: Embedder) -> None:
    """Refuse an embedder whose model revision differs from the one that
    made the stored vectors (vectors of two revisions are not comparable).
    The first vectors written record the revision. An embedder without a
    revision (the fake one) is not checked."""
    if not embedder.revision:
        return
    row = conn.execute("SELECT value FROM meta WHERE name = 'embedding_revision'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (name, value) VALUES ('embedding_revision', %s)",
                     (embedder.revision,))
    elif row["value"] != embedder.revision:
        raise RuntimeError(
            f"the collection was embedded with model revision {row['value']}, but this"
            f" embedder has revision {embedder.revision}; vectors of different revisions"
            " cannot be compared")


def embed_pending_items(conn: psycopg.Connection, embedder: Embedder,
                        batch_size: int = 32) -> int:
    """Give every item without a vector one, a batch at a time, each batch
    committed; returns how many were embedded."""
    check_revision(conn, embedder)
    done = 0
    while True:
        rows = conn.execute("SELECT key, title, abstract FROM items WHERE embedding IS NULL"
                            " ORDER BY key LIMIT %s", (batch_size,)).fetchall()
        if not rows:
            conn.commit()
            return done
        vectors = embedder.embed([item_document(r["title"], r["abstract"]) for r in rows])
        conn.cursor().executemany(
            "UPDATE items SET embedding = %s WHERE key = %s",
            [(vector_literal(v), r["key"]) for r, v in zip(rows, vectors, strict=True)])
        conn.commit()
        done += len(rows)


def embed_pending_passages(conn: psycopg.Connection, embedder: Embedder,
                           batch_size: int = 32) -> int:
    """Give every passage without a vector one, except artefacts, which
    never get one; returns how many were embedded."""
    check_revision(conn, embedder)
    done = 0
    while True:
        rows = conn.execute(
            "SELECT item, idx, coalesce(embed_text, text) AS text FROM passages"
            " WHERE embedding IS NULL AND NOT artefact ORDER BY item, idx LIMIT %s",
            (batch_size,)).fetchall()
        if not rows:
            conn.commit()
            return done
        vectors = embedder.embed([r["text"] for r in rows])
        conn.cursor().executemany(
            "UPDATE passages SET embedding = %s WHERE item = %s AND idx = %s",
            [(vector_literal(v), r["item"], r["idx"]) for r, v in zip(rows, vectors, strict=True)])
        conn.commit()
        done += len(rows)
