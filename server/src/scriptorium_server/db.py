"""The database: connections and schema migrations.

The schema is a series of numbered SQL files in migrations/. Each is
applied once, in order, inside its own transaction, and recorded in the
table schema_migrations; migrate() applies whatever is not recorded yet.
"""

from importlib import resources

import psycopg
from psycopg.rows import dict_row

from scriptorium_server import settings

_MIGRATION_LOCK = 7_267_478_001  # an arbitrary number naming the advisory lock


def connect(address: str | None = None, schema: str | None = None) -> psycopg.Connection:
    """A connection whose rows are dicts. The address defaults to the
    database.address setting. `schema` puts the tables in a schema of that
    name instead of the default one (the tests use one schema per test)."""
    address = address or str(settings.load()["database.address"])
    options = f"-c search_path={schema},public" if schema else None
    return psycopg.connect(address, row_factory=dict_row, options=options)


def migration_files() -> list[tuple[str, str]]:
    """(name, SQL) of every migration, in order."""
    folder = resources.files("scriptorium_server") / "migrations"
    files = sorted(f for f in folder.iterdir() if f.name.endswith(".sql"))
    return [(f.name, f.read_text()) for f in files]


def migrate(conn: psycopg.Connection, only: set[str] | None = None) -> list[str]:
    """Apply the migrations not yet applied; returns their names. Safe to
    run at every start and from two processes at once (the second waits).
    `only` limits it to the migrations of those names (a restore builds the
    schema a backup was made with before loading it)."""
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
        for name, sql in migration_files():
            if name in done or (only is not None and name not in only):
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
            applied.append(name)
    return applied
