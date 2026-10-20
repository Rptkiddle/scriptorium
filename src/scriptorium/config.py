"""Central configuration for scriptorium.

All paths, model names, and service endpoints live here. Values can be
overridden with environment variables (SCRIPTORIUM_* prefix) so the same code
runs on other machines without edits.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Contact address sent with polite-pool API requests (Crossref and
# OpenAlex identify heavy anonymous users; a mailto gets better service).
CONTACT_EMAIL = "r.t.kiddle@vu.nl"


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


@dataclass(frozen=True)
class Config:
    # Zotero
    zotero_local_api: str = os.environ.get(
        "SCRIPTORIUM_ZOTERO_API", "http://localhost:23119/api"
    )
    zotero_data_dir: Path = field(
        default_factory=lambda: _env_path(
            "SCRIPTORIUM_ZOTERO_DATA", Path("~/Desktop/libr/Zotero").expanduser()
        )
    )

    # Index storage (the repo's `data` symlink points at the .nosync data dir)
    index_dir: Path = field(
        default_factory=lambda: _env_path("SCRIPTORIUM_INDEX_DIR", _REPO_ROOT / "data")
    )

    # Models (local, MPS)
    embedding_model: str = os.environ.get(
        "SCRIPTORIUM_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B"
    )
    # Matryoshka truncation: vectors are cut to this many dimensions before
    # normalisation. Changing it requires migrating or rebuilding the index
    # (see scripts/migrate_embedding_dim.py).
    embedding_dim: int = int(os.environ.get("SCRIPTORIUM_EMBEDDING_DIM", "256"))

    # Chunking (paperqa defaults)
    chunk_chars: int = int(os.environ.get("SCRIPTORIUM_CHUNK_CHARS", "5000"))
    chunk_overlap: int = int(os.environ.get("SCRIPTORIUM_CHUNK_OVERLAP", "250"))

    # Daemon behaviour
    port: int = int(os.environ.get("SCRIPTORIUM_PORT", "8765"))


def load_config() -> Config:
    return Config()
