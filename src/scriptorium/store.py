"""The index's storage: one SQLite file, and the vectors searched in memory.

Everything the index holds lives in `index.sqlite` in the data directory:
the catalogue (one row per Zotero item, with its title-and-abstract
vector), the full-text chunks (text, page range and vector), the sync
bookkeeping and the embedding fingerprint. Writes that belong together go
in one transaction, so a crash never leaves the bookkeeping out of step
with the vectors.

Vectors are stored as float16 (2 bytes a number; a 256-dimension vector
is 512 bytes). Search is exact: the query is compared with every admitted
vector in one matrix product (VectorMatrix), and only the vectors are
held in memory, while searching. Metadata filters are
SQL over the item table and its multi-value table (FilterSQL), which
narrow the rows before the comparison.
"""

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

STORE_FILE = "index.sqlite"
# A schema change ships with a migration or requires a rebuild; an index
# with a different number is refused, never read on a guess.
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- The catalogue: one row per Zotero item. `payload` holds the stored
-- fields as a JSON object (index.catalogue_fields); the columns beside it
-- are copies of the fields filters and sorts read, so SQL can use them.
CREATE TABLE items(
    item_key     TEXT PRIMARY KEY,
    version      INTEGER NOT NULL,          -- Zotero version catalogued
    has_pdf      INTEGER NOT NULL,          -- PDF present when catalogued
    citation_key TEXT NOT NULL DEFAULT '',
    item_type    TEXT NOT NULL DEFAULT '',
    year_int     INTEGER,
    venue_lc     TEXT NOT NULL DEFAULT '',
    added_ts     INTEGER,
    title_norm   TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL,
    vec          BLOB NOT NULL              -- float16, embedding_dim numbers
);
CREATE INDEX items_citation_key ON items(citation_key);
CREATE INDEX items_title_norm ON items(title_norm);

-- Multi-valued fields of an item, one row per value: creator (name keys),
-- tag, collection (keys), category / publisher / venue_folder (folder
-- names), identifier / preprint_identifier ("doi:..." / "arxiv:...").
-- value_lc is the case-folded value filters compare with.
CREATE TABLE item_values(
    item_key TEXT NOT NULL REFERENCES items(item_key) ON DELETE CASCADE,
    field    TEXT NOT NULL,
    value    TEXT NOT NULL,
    value_lc TEXT NOT NULL
);
CREATE INDEX item_values_by_value ON item_values(field, value);
CREATE INDEX item_values_by_value_lc ON item_values(field, value_lc);
CREATE INDEX item_values_by_item ON item_values(item_key);

-- Full-text passages (tei.passages), numbered in reading order: body,
-- then footnotes, then appendix. Metadata (citation, creators, year,
-- venue) is not copied here: it is read from the item's catalogue row, so
-- an edit in Zotero changes one row, not every passage. An artefact
-- (a table or formula read as text) is stored without a vector, so
-- searches never return it.
CREATE TABLE chunks(
    id          INTEGER PRIMARY KEY,
    item_key    TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    part        TEXT NOT NULL DEFAULT 'body',  -- body | footnote | appendix
    pages       TEXT NOT NULL,              -- e.g. "3-5"
    text        TEXT NOT NULL,
    artefact    INTEGER NOT NULL DEFAULT 0,
    vec         BLOB,                       -- NULL for an artefact
    UNIQUE(item_key, chunk_index)
);

-- Sync bookkeeping (index.SyncState): papers with indexed full text, and
-- items whose PDF could not be read.
CREATE TABLE papers(
    item_key     TEXT PRIMARY KEY,
    version      INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    chunks       INTEGER NOT NULL,
    parser       INTEGER,
    meta         INTEGER
);
CREATE TABLE failures(
    item_key TEXT PRIMARY KEY,
    reason   TEXT NOT NULL,
    attempt  TEXT NOT NULL DEFAULT ''
);
"""

# The multi-value fields above, and the catalogue_fields() key each is read from.
VALUE_FIELDS = {
    "creator": "creator_surnames",
    "tag": "tags",
    "collection": "collections",
    "category": "categories",
    "publisher": "publishers",
    "venue_folder": "venue_folders",
    "identifier": "identifiers",
    "preprint_identifier": "preprint_identifiers",
}


def to_blob(vector) -> bytes:
    return np.asarray(vector, dtype=np.float16).tobytes()


def from_blob(blob: bytes) -> list[float]:
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32).tolist()


class Store:
    """The SQLite file. One connection serves every thread, guarded by
    `lock` (calls are short; embedding and scoring happen outside it).
    WAL journaling lets a crash lose at most the transaction in flight."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Counts of vector writes, so in-memory matrices know when they are stale.
        self.generation = {"chunks": 0, "items": 0}
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                with self.transaction():
                    # Statement by statement (executescript would commit
                    # outside the transaction), comment lines removed first.
                    code = "\n".join(line for line in _SCHEMA.splitlines()
                                     if not line.lstrip().startswith("--"))
                    for statement in code.split(";"):
                        if statement.strip():
                            self.conn.execute(statement)
                    self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} has index schema {version}; this scriptorium reads"
                    f" schema {SCHEMA_VERSION}. Rebuild the index (move the file aside"
                    " and run a sync).")

    def transaction(self):
        return _Transaction(self)

    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def meta(self, key: str, default: str = "") -> str:
        row = self.query("SELECT value FROM meta WHERE key = ?", (key,))
        return row[0]["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        self.execute("INSERT INTO meta(key, value) VALUES(?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class _Transaction:
    """BEGIN IMMEDIATE ... COMMIT under the store's lock; ROLLBACK on error.
    Nested use joins the outer transaction."""

    def __init__(self, store: Store):
        self.store = store
        self.outer = False

    def __enter__(self):
        self.store.lock.acquire()
        self.outer = not self.store.conn.in_transaction
        if self.outer:
            self.store.conn.execute("BEGIN IMMEDIATE")
        return self.store

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.outer:
                self.store.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.store.lock.release()
        return False


# -- filters as SQL ------------------------------------------------------------

@dataclass
class FilterSQL:
    """A WHERE clause over `items AS i` and its parameters. Empty `where`
    admits every item."""

    where: str
    params: list

    def items_sql(self, columns: str = "i.item_key") -> tuple[str, list]:
        sql = f"SELECT {columns} FROM items AS i"
        return (sql + (f" WHERE {self.where}" if self.where else ""), list(self.params))


def any_value(field: str, values: list[str], column: str = "value_lc") -> tuple[str, list]:
    """An item has any of `values` in a multi-value field. column "value"
    compares exactly (tags, collection keys, name keys), "value_lc" with
    the case-folded value (folder names)."""
    marks = ", ".join("?" * len(values))
    sql = (f"EXISTS (SELECT 1 FROM item_values v WHERE v.item_key = i.item_key"
           f" AND v.field = ? AND v.{column} IN ({marks}))")
    return sql, [field, *values]


# -- the in-memory vectors -----------------------------------------------------

class VectorMatrix:
    """Vectors as stored (float16, one row each) with their row ids and
    item keys; scored as float32 in blocks, so a large matrix never needs
    a float32 copy of itself. Each row is normalised as it is scored, so
    scores are exact cosine similarities."""

    BLOCK = 4096

    def __init__(self, ids: np.ndarray, items: list[str], vectors: np.ndarray, generation: int):
        self.ids = ids              # int64 row ids (chunk id; item rowid)
        self.items = items          # item key of each row
        self.vectors = vectors      # float16, rows x dim
        self.generation = generation
        # Item keys as small integers, so filtering rows by item is fast.
        self._code: dict[str, int] = {}
        self._codes = np.fromiter((self._code.setdefault(k, len(self._code)) for k in items),
                                  np.int32, len(items))

    @classmethod
    def load(cls, rows: list, dim: int, generation: int) -> "VectorMatrix":
        """From rows of (id, item_key, vec blob)."""
        if not rows:
            return cls(np.zeros(0, np.int64), [], np.zeros((0, dim), np.float16), generation)
        ids = np.fromiter((r[0] for r in rows), np.int64, len(rows))
        items = [r[1] for r in rows]
        vectors = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float16)
        return cls(ids, items, vectors.reshape(len(rows), dim), generation)

    @property
    def nbytes(self) -> int:
        return self.vectors.nbytes

    def rows_of(self, item_keys: set[str] | None) -> np.ndarray:
        """Row positions whose item is admitted (all rows when None)."""
        if item_keys is None:
            return np.arange(len(self.ids))
        codes = [self._code[k] for k in item_keys if k in self._code]
        return np.flatnonzero(np.isin(self._codes, codes))

    def top(self, query, k: int, rows: np.ndarray) -> list[tuple[int, float]]:
        """The k best (row position, cosine) among `rows`, best first."""
        if k <= 0 or len(rows) == 0:
            return []
        q = np.asarray(query, dtype=np.float32)
        q = q / (np.linalg.norm(q) or 1.0)
        scores = np.empty(len(rows), dtype=np.float32)
        for start in range(0, len(rows), self.BLOCK):
            block = self.vectors[rows[start : start + self.BLOCK]].astype(np.float32)
            norms = np.linalg.norm(block, axis=1)
            norms[norms == 0] = 1.0
            scores[start : start + len(block)] = (block @ q) / norms
        if len(rows) > k:
            best = np.argpartition(-scores, k - 1)[:k]
        else:
            best = np.arange(len(rows))
        best = best[np.lexsort((rows[best], -scores[best]))]  # score, then row order
        return [(int(rows[b]), float(scores[b])) for b in best]


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
