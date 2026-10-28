"""Central configuration for scriptorium.

All paths, model names, and service endpoints live here. Values can be
overridden with environment variables (SCRIPTORIUM_* prefix) so the same code
runs on other machines without edits.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


@dataclass(frozen=True)
class Config:
    # Zotero
    zotero_local_api: str = os.environ.get(
        "SCRIPTORIUM_ZOTERO_API", "http://localhost:23119/api"
    )

    # Index storage (the data folder; the repo's git-ignored `data` link may point elsewhere)
    index_dir: Path = field(
        default_factory=lambda: _env_path("SCRIPTORIUM_INDEX_DIR", _REPO_ROOT / "data")
    )

    # Models (local, MPS)
    embedding_model: str = os.environ.get(
        "SCRIPTORIUM_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B"
    )
    # Matryoshka truncation: vectors are cut to this many dimensions before
    # normalisation. The index records it; changing it requires a rebuild.
    embedding_dim: int = int(os.environ.get("SCRIPTORIUM_EMBEDDING_DIM", "256"))

    # Daemon behaviour
    port: int = int(os.environ.get("SCRIPTORIUM_PORT", "8765"))


def load_config() -> Config:
    return Config()
