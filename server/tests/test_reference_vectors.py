"""The local embedder reproduces the stored reference vectors
(tests/data/reference_vectors.json).

A library update (sentence-transformers, transformers, torch) or a new
model revision can change the vectors silently; the index would then mix
vectors from two models. Skipped when the model is not in the local cache.
"""

import json
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.model  # loads the real model: deselect with -m 'not model'

from scriptorium_server.embedding import DIM, MODEL, RealEmbedder

REFERENCE_FILE = Path(__file__).parent / "data" / "reference_vectors.json"
if not REFERENCE_FILE.exists():
    pytest.skip("no reference vectors", allow_module_level=True)
REFERENCE = json.loads(REFERENCE_FILE.read_text())
MIN_COSINE = 0.999


@pytest.fixture(scope="module")
def embedder():
    emb = RealEmbedder()
    if not emb.revision:
        pytest.skip("embedding model not in the local Hugging Face cache")
    return emb


def cosines(stored, fresh):
    a, b = np.asarray(stored, np.float32), np.asarray(fresh, np.float32)
    return (a * b).sum(1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))


def test_reference_vectors_come_from_the_configured_model(embedder):
    assert REFERENCE["model"] == MODEL
    assert REFERENCE["dim"] == DIM
    assert embedder.revision == REFERENCE["revision"], (
        "the cached model revision differs from the one the reference vectors (and the"
        " index) were made with")


def test_the_local_embedder_reproduces_documents_and_queries(embedder):
    docs = REFERENCE["documents"]
    fresh = embedder.embed([d["text"] for d in docs])
    assert cosines([d["vector"] for d in docs], fresh).min() >= MIN_COSINE
    queries = REFERENCE["queries"]
    fresh = [embedder.embed_query(q["text"]) for q in queries]
    assert cosines([q["vector"] for q in queries], fresh).min() >= MIN_COSINE
