"""The local embedder reproduces the stored reference vectors
(tests/data/reference_vectors.json, made by scripts/make_reference_vectors.py).

A library update (sentence-transformers, transformers, torch) or a new
model revision can change the vectors silently; the index would then mix
vectors from two models. Skipped when the model is not in the local cache.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from scriptorium.config import Config
from scriptorium.index import Embedder

REFERENCE = json.loads((Path(__file__).parent / "data" / "reference_vectors.json").read_text())
MIN_COSINE = 0.999


@pytest.fixture(scope="module")
def embedder():
    cfg = Config()
    emb = Embedder(cfg.embedding_model, cfg.embedding_dim)
    if not emb.revision:
        pytest.skip("embedding model not in the local Hugging Face cache")
    return emb


def cosines(stored, fresh):
    a, b = np.asarray(stored, np.float32), np.asarray(fresh, np.float32)
    return (a * b).sum(1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))


def test_reference_vectors_come_from_the_configured_model(embedder):
    assert REFERENCE["model"] == Config().embedding_model
    assert REFERENCE["dim"] == Config().embedding_dim
    assert embedder.revision == REFERENCE["revision"], (
        "the cached model revision differs from the one the reference vectors (and the"
        " index) were made with")


def test_the_local_embedder_reproduces_documents_and_queries(embedder):
    docs = REFERENCE["documents"]
    fresh = embedder.encode_documents([d["text"] for d in docs])
    assert cosines([d["vector"] for d in docs], fresh).min() >= MIN_COSINE
    queries = REFERENCE["queries"]
    fresh = [embedder.encode_query(q["text"]) for q in queries]
    assert cosines([q["vector"] for q in queries], fresh).min() >= MIN_COSINE
