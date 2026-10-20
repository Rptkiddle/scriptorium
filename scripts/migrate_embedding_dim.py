"""Migrate the index to the configured embedding dimension by truncation.

Qwen3-Embedding is Matryoshka-trained, so existing vectors can be cut to
their leading dimensions and renormalised — no re-embedding, no model load.
Point IDs, payloads, and state.json are untouched, so no sync is triggered.

Run with the daemon STOPPED (embedded Qdrant is single-process):

    uv run python scripts/migrate_embedding_dim.py

Reads the target dimension from config (SCRIPTORIUM_EMBEDDING_DIM). Back up
the qdrant/ directory before running; the migration rebuilds the collection
in place.
"""

import math
import sys

from qdrant_client import QdrantClient, models

from scriptorium.cli import daemon_running
from scriptorium.config import load_config
from scriptorium.index import COLLECTION

BATCH = 256


def main() -> None:
    cfg = load_config()
    if daemon_running(cfg.port):
        sys.exit("The scriptorium daemon is running and owns the index — stop it first.")
    target = cfg.embedding_dim
    client = QdrantClient(path=str(cfg.index_dir / "qdrant"))

    if not client.collection_exists(COLLECTION):
        sys.exit(f"Collection {COLLECTION!r} does not exist — nothing to migrate.")
    info = client.get_collection(COLLECTION)
    stored_dim = info.config.params.vectors.size
    total = client.count(COLLECTION).count
    print(f"Collection: {total} points at {stored_dim} dims; target {target} dims.")
    if stored_dim == target:
        sys.exit("Already at the target dimension — nothing to do.")
    if stored_dim < target:
        sys.exit("Target exceeds stored dimension — truncation impossible; re-embed instead.")

    # Read everything into memory before touching the collection.
    points: list[models.PointStruct] = []
    offset = None
    while True:
        batch, offset = client.scroll(
            COLLECTION, limit=BATCH, offset=offset, with_payload=True, with_vectors=True
        )
        for p in batch:
            head = p.vector[:target]
            norm = math.sqrt(sum(x * x for x in head))
            points.append(
                models.PointStruct(
                    id=p.id, vector=[x / norm for x in head], payload=p.payload
                )
            )
        if offset is None:
            break
    if len(points) != total:
        sys.exit(f"Read {len(points)} points but count reports {total} — aborting.")
    print(f"Read {len(points)} points; rebuilding collection at {target} dims.")

    client.delete_collection(COLLECTION)
    client.create_collection(
        COLLECTION,
        vectors_config=models.VectorParams(size=target, distance=models.Distance.COSINE),
    )
    for i in range(0, len(points), BATCH):
        client.upsert(COLLECTION, points=points[i : i + BATCH])

    new_total = client.count(COLLECTION).count
    new_dim = client.get_collection(COLLECTION).config.params.vectors.size
    client.close()
    if new_total != total:
        sys.exit(f"MISMATCH after rebuild: {new_total} points vs {total} before.")
    print(f"Done: {new_total} points at {new_dim} dims.")



if __name__ == "__main__":
    main()
