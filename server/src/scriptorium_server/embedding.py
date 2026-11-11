"""Embedding of item texts (title and abstract) and passages.

Documents are encoded plain; queries carry the model's instruction prompt
(the asymmetric scheme the model is trained for). Model and dimension are
fixed here because every stored vector depends on them: changing either
means embedding the whole collection again.
"""

import threading
import time

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
    """Lazy-loading sentence-transformers wrapper (document/query modes).

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

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            elif torch.backends.mps.is_available():
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
