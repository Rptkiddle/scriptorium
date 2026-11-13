"""Backup and restore of the server's data.

A backup is written to the backup folder (setting backup.folder):

    runs/<YYYY-MM-DD-HHMMSS>/   one folder per backup: every table of the
                                database as tables/<table>.copy.gz, and
                                manifest.json (written last: a folder
                                without it is an unfinished backup)
    files/pdfs/, files/tei/     one copy of the held PDFs and the parsed
                                full texts, added to by each backup

The tables are read in one transaction, so a backup is the database as it
was at one moment while the server keeps running. Run folders older than
backup.days_kept days are removed; the newest finished one always stays.

PDFs and parsed texts are only ever added to files/: a file removed from
the collection stays in the backup until it is removed there by hand.
Not backed up: donations still waiting to be processed (the holding
area), run folders, the embedding model, the settings file.

A restore fills an empty database and an empty data folder from one
backup: the schema is built by the migrations the backup was made with,
the tables are loaded, later migrations are applied, and the PDFs the
database refers to and the parsed texts are copied back.
"""

import gzip
import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import sql

from scriptorium_server import __version__, db

MANIFEST = "manifest.json"
STAMP = "%Y-%m-%d-%H%M%S"
STORES = ("pdfs", "tei")  # folders of the data folder that are copied
_BLOCK = 1 << 20


class BackupError(RuntimeError):
    """A backup that cannot be restored, or a target that is not empty."""


# -- the database's shape ---------------------------------------------------------


def _tables(conn: psycopg.Connection) -> list[str]:
    """The tables of the connection's schema, each after the tables it refers to."""
    names = {r["relname"] for r in conn.execute(
        "SELECT c.relname FROM pg_class c WHERE c.relnamespace = current_schema()::regnamespace"
        " AND c.relkind = 'r'")}
    needs: dict[str, set[str]] = {name: set() for name in names}
    for r in conn.execute(
            "SELECT c.relname AS child, p.relname AS parent FROM pg_constraint k"
            " JOIN pg_class c ON c.oid = k.conrelid JOIN pg_class p ON p.oid = k.confrelid"
            " WHERE k.contype = 'f' AND c.relnamespace = current_schema()::regnamespace"):
        if r["child"] != r["parent"] and r["parent"] in names:
            needs[r["child"]].add(r["parent"])
    ordered: list[str] = []
    while needs:
        ready = sorted(name for name, parents in needs.items() if parents <= set(ordered))
        if not ready:
            raise BackupError("tables refer to each other in a circle: " + ", ".join(sorted(needs)))
        ordered += ready
        for name in ready:
            del needs[name]
    return ordered


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    """The table's stored columns; a column the database computes is left out."""
    return [r["attname"] for r in conn.execute(
        "SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0"
        " AND NOT a.attisdropped AND a.attgenerated = '' ORDER BY a.attnum", (table,))]


def _count(conn: psycopg.Connection, table: str) -> int:
    return conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(
        sql.Identifier(table))).fetchone()["n"]


# -- files ------------------------------------------------------------------------


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.part")
    shutil.copyfile(source, part)
    os.replace(part, target)


def _mirror(source: Path, target: Path) -> tuple[int, int]:
    """Copy every file of `source` that `target` lacks or holds in another
    size; returns (files in source, files copied). Names starting with a
    dot (files still being written) are left out."""
    seen = copied = 0
    if not source.is_dir():
        return 0, 0
    for folder, _, names in os.walk(source):
        for name in names:
            if name.startswith("."):
                continue
            seen += 1
            src = Path(folder) / name
            dst = target / src.relative_to(source)
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                _copy_file(src, dst)
                copied += 1
    return seen, copied


# -- backup -----------------------------------------------------------------------


def runs(folder: Path) -> list[Path]:
    """The finished backups in the backup folder, oldest first."""
    base = Path(folder) / "runs"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if (p / MANIFEST).is_file())


def _made(run: Path) -> datetime | None:
    try:
        return datetime.strptime(run.name[:17], STAMP)
    except ValueError:
        return None


def prune(folder: Path, days_kept: int, now: datetime | None = None) -> list[str]:
    """Remove run folders older than `days_kept` days, and unfinished ones
    older than a day; the newest finished backup always stays. Returns the
    names removed."""
    now = now or datetime.now()
    base = Path(folder) / "runs"
    finished = runs(folder)
    newest = finished[-1] if finished else None
    removed = []
    for run in sorted(base.iterdir()) if base.is_dir() else []:
        made = _made(run)
        if made is None or run == newest or not run.is_dir():
            continue
        limit = timedelta(days=days_kept if run in finished else 1)
        if now - made > limit:
            shutil.rmtree(run)
            removed.append(run.name)
    return removed


def backup(conn: psycopg.Connection, data: Path, folder: Path, days_kept: int,
           now: datetime | None = None) -> dict:
    """Write one backup of the database behind `conn` and the files under
    `data` to `folder`, then remove old backups. Returns the manifest, with
    "run" (the backup's folder), "copied" and "removed" added."""
    now = now or datetime.now()
    data, folder = Path(data), Path(folder)
    run = folder / "runs" / now.strftime(STAMP)
    (run / "tables").mkdir(parents=True)
    conn.rollback()
    previous = conn.isolation_level
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    tables: dict[str, dict] = {}
    try:
        with conn.transaction():
            migrations = [r["name"] for r in conn.execute(
                "SELECT name FROM schema_migrations ORDER BY name")]
            for table in _tables(conn):
                if table == "schema_migrations":
                    continue
                columns = _columns(conn, table)
                statement = sql.SQL("COPY {} ({}) TO STDOUT").format(
                    sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns)))
                with gzip.open(run / "tables" / f"{table}.copy.gz", "wb", compresslevel=4) as out:
                    with conn.cursor().copy(statement) as copy:
                        for block in copy:
                            out.write(block)
                tables[table] = {"columns": columns, "rows": _count(conn, table)}
    finally:
        conn.rollback()
        conn.isolation_level = previous
    files, copied = {}, {}
    for store in STORES:
        files[store], copied[store] = _mirror(data / store, folder / "files" / store)
    manifest = {"made": now.isoformat(timespec="seconds"), "server": __version__,
                "commit": os.environ.get("SCRIPTORIUM_COMMIT", ""), "migrations": migrations,
                "tables": tables, "files": files}
    part = run / f"{MANIFEST}.part"
    part.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(part, run / MANIFEST)
    return {**manifest, "run": str(run), "copied": copied,
            "removed": prune(folder, days_kept, now)}


def summary_line(result: dict) -> str:
    rows = sum(t["rows"] for t in result["tables"].values())
    return (f"Backup {Path(result['run']).name}: {len(result['tables'])} tables, {rows} rows;"
            f" {result['files']['pdfs']} PDFs ({result['copied']['pdfs']} new),"
            f" {result['files']['tei']} parsed-text files ({result['copied']['tei']} new);"
            f" {len(result['removed'])} old backup(s) removed")


# -- restore ----------------------------------------------------------------------


def find(folder: Path, name: str) -> Path:
    """The backup called `name` ("latest" for the newest). BackupError when
    there is none."""
    finished = runs(folder)
    if name == "latest":
        if not finished:
            raise BackupError(f"no finished backup in {folder}")
        return finished[-1]
    for run in finished:
        if run.name == name:
            return run
    raise BackupError(f"no finished backup named {name!r} in {folder}; there are: "
                      + (", ".join(r.name for r in finished) or "none"))


def _empty(folder: Path) -> bool:
    return not folder.exists() or not any(folder.iterdir())


def restore(conn: psycopg.Connection, run: Path, data: Path) -> dict:
    """Fill the empty database behind `conn` and the empty data folder
    `data` from the backup in `run`. Returns {"tables": {name: rows},
    "pdfs": n, "tei": n}. BackupError when the database holds tables, the
    data folder holds PDFs or parsed texts, the backup was made by a newer
    server, or what was restored differs from what the backup recorded."""
    run, data = Path(run), Path(data)
    if not (run / MANIFEST).is_file():
        raise BackupError(f"{run} is not a finished backup (no {MANIFEST})")
    manifest = json.loads((run / MANIFEST).read_text())
    conn.rollback()
    if _tables(conn):
        raise BackupError("the database is not empty; a backup is restored into an empty one")
    for store in STORES:
        if not _empty(data / store):
            raise BackupError(f"{data / store} is not empty; a backup is restored into an"
                              " empty data folder")
    known = [name for name, _ in db.migration_files()]
    made_with = manifest["migrations"]
    if made_with != known[:len(made_with)]:
        raise BackupError("the backup was made by a server with other migrations ("
                          + ", ".join(made_with) + "); restore it with that server's version")
    files_root = run.parent.parent / "files"
    restored: dict[str, int] = {}
    conn.rollback()  # the checks above opened a transaction; the restore is one of its own
    with conn.transaction():
        db.migrate(conn, only=set(made_with))
        present = set(_tables(conn))
        for table in _tables(conn):
            if table == "schema_migrations":
                continue
            entry = manifest["tables"].get(table)
            if entry is None:
                continue
            statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, entry["columns"])))
            with gzip.open(run / "tables" / f"{table}.copy.gz", "rb") as source:
                with conn.cursor().copy(statement) as copy:
                    while block := source.read(_BLOCK):
                        copy.write(block)
            restored[table] = _count(conn, table)
            if restored[table] != entry["rows"]:
                raise BackupError(f"table {table}: {restored[table]} rows restored, the backup"
                                  f" recorded {entry['rows']}")
        missing = set(manifest["tables"]) - present
        if missing:
            raise BackupError("the backup holds tables this server does not have: "
                              + ", ".join(sorted(missing)))
        # Numbering continues after the highest number restored.
        for r in conn.execute(
                "SELECT c.relname AS tbl, a.attname AS col FROM pg_attribute a"
                " JOIN pg_class c ON c.oid = a.attrelid"
                " WHERE c.relnamespace = current_schema()::regnamespace AND c.relkind = 'r'"
                " AND a.attidentity <> '' AND NOT a.attisdropped").fetchall():
            conn.execute(sql.SQL(
                "SELECT setval(pg_get_serial_sequence(%s, %s),"
                " (SELECT coalesce(max({col}), 0) + 1 FROM {tbl}), false)").format(
                    col=sql.Identifier(r["col"]), tbl=sql.Identifier(r["tbl"])),
                (f'"{r["tbl"]}"', r["col"]))
        db.migrate(conn)
        hashes = [r["sha256"] for r in conn.execute("SELECT DISTINCT sha256 FROM files")]
    pdfs = 0
    for sha256 in hashes:
        relative = Path(sha256[:2]) / f"{sha256}.pdf"
        source = files_root / "pdfs" / relative
        if not source.is_file():
            raise BackupError(f"the backup lacks the PDF {relative}, which its database refers to")
        _copy_file(source, data / "pdfs" / relative)
        pdfs += 1
    tei, _ = _mirror(files_root / "tei", data / "tei")
    return {"tables": restored, "pdfs": pdfs, "tei": tei}
