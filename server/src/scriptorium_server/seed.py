"""The seed import: a member's existing library, taken in full from the
personal version of Scriptorium.

Two inputs, both made on the member's own machine:

- the personal version's index (index.sqlite): which items there are, their
  folder placement, their vectors, and the passages of every paper with a
  parsed PDF, with their vectors;
- a records file (one JSON object per line) of the items' full
  bibliographic records, written by export_records() from Zotero's local
  API. Tags, collections and notes are left out when it is written.

The items are verified already and one per publication, so they are
created as they are, with provenance "donation": no search for other
versions. Vectors and passages are copied unchanged, since the embedding
model and the passage rules are the same. An item whose key is in the
collection already is skipped, so the import can be run again; an item
whose DOI or arXiv ID another item answers to is skipped and listed.

The PDFs themselves arrive separately (`seed-files`); the import records
each full text's PDF hash in pending_files so the file is recognised when
it comes. Running the import again also restores that record for an item
already present whose PDF has not arrived, should it be missing.
"""

import json
import sqlite3
import struct
from collections.abc import Iterator
from pathlib import Path

import psycopg
import requests

from scriptorium_server import items, jobs
from scriptorium_server.embedding import DIM, MODEL
from scriptorium_server.identity import own_identifiers

ZOTERO_API = "http://localhost:23119/api"
_NOT_ITEMS = {"attachment", "note", "annotation"}
_LEFT_OUT = {"tags", "collections", "relations", "version", "dateModified"}
_VENUE_FIELD = {"journalArticle": "publicationTitle", "conferencePaper": "proceedingsTitle",
                "preprint": "repository", "bookSection": "bookTitle"}
_BATCH = 500


# --- on the member's machine: the records file ---------------------------------


def zotero_items(api: str = ZOTERO_API, session: requests.Session | None = None) -> Iterator[dict]:
    """Every top-level item's data from Zotero's local API, a page at a time."""
    session = session or requests.Session()
    start = 0
    while True:
        response = session.get(f"{api}/users/0/items/top",
                               params={"limit": 100, "start": start, "format": "json"},
                               timeout=120)
        response.raise_for_status()
        page = response.json()
        if not page:
            return
        for entry in page:
            data = entry.get("data") or {}
            if data.get("itemType") not in _NOT_ITEMS and not data.get("parentItem"):
                yield data
        start += len(page)


def export_records(out: Path, api: str = ZOTERO_API,
                   session: requests.Session | None = None) -> int:
    """Write the records file: one item per line, without tags, collections
    or relations; returns how many. Reads only; Zotero must be running."""
    count = 0
    with open(out, "w") as f:
        for data in zotero_items(api, session):
            record = {k: v for k, v in data.items() if k not in _LEFT_OUT and v not in ("", [])}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


# --- on the server: the import -------------------------------------------------


def _vector(blob: bytes) -> str:
    """A float16 blob from the index as PostgreSQL reads a vector."""
    return "[" + ",".join(repr(x) for x in struct.unpack(f"<{DIM}e", blob)) + "]"


def _creators(authors: list[str]) -> list[dict]:
    """Full names from the index as creators, split at the last space."""
    creators = []
    for name in authors:
        first, _, last = name.strip().rpartition(" ")
        creators.append({"creatorType": "author", "firstName": first, "lastName": last}
                        if first else {"creatorType": "author", "name": last})
    return creators


def _record_from_index(payload: dict) -> dict:
    """A record from what the index holds, for an item the records file lacks."""
    item_type = payload.get("item_type") or "journalArticle"
    record = {
        "itemType": item_type, "title": payload.get("title", ""),
        "creators": _creators(payload.get("authors") or []),
        "date": payload.get("year", ""), "abstractNote": payload.get("abstract", ""),
    }
    for ident in payload.get("identifiers") or []:
        kind, _, value = ident.partition(":")
        record["DOI" if kind == "doi" else "archiveID"] = value if kind == "doi" else f"arXiv:{value}"
    lines = [("arXiv: " if i.startswith("arxiv:") else "Preprint: ") + i.partition(":")[2]
             for i in payload.get("preprint_identifiers") or []]
    if lines:
        record["extra"] = "\n".join(lines)
    if payload.get("venue") and item_type in _VENUE_FIELD:
        record[_VENUE_FIELD[item_type]] = payload["venue"]
    return record


def _load_records(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    records = {}
    with open(path) as f:
        for line in f:
            if line.strip():
                data = json.loads(line)
                records[data["key"]] = {k: v for k, v in data.items() if k not in _LEFT_OUT}
    return records


def _check_model(conn: psycopg.Connection, source: sqlite3.Connection) -> None:
    """The index's vectors must come from this server's model and, where
    vectors exist here already, from the same revision."""
    meta = dict(source.execute("SELECT key, value FROM meta"))
    if meta.get("embedding_model") != MODEL or int(meta.get("embedding_dim", 0)) != DIM:
        raise ValueError(f"the index was embedded with {meta.get('embedding_model')}"
                         f" ({meta.get('embedding_dim')} dimensions); this server uses"
                         f" {MODEL} ({DIM})")
    revision = meta.get("embedding_revision", "")
    row = conn.execute("SELECT value FROM meta WHERE name = 'embedding_revision'").fetchone()
    if row and revision and row["value"] != revision:
        raise ValueError(f"the index's vectors are of model revision {revision}; the"
                         f" collection's are of {row['value']}")
    if revision and not row:
        conn.execute("INSERT INTO meta (name, value) VALUES ('embedding_revision', %s)",
                     (revision,))


def _copy_passages(conn: psycopg.Connection, source: sqlite3.Connection, key: str) -> int:
    rows = source.execute("SELECT chunk_index, part, pages, text, artefact, vec FROM chunks"
                          " WHERE item_key = ? ORDER BY chunk_index", (key,)).fetchall()
    with conn.cursor().copy("COPY passages (item, idx, part, pages, text, artefact, embedding)"
                            " FROM STDIN") as copy:
        for idx, part, pages, text, artefact, vec in rows:
            copy.write_row((key, idx, part, pages, text, bool(artefact),
                            _vector(vec) if vec is not None else None))
    return len(rows)


def _await_file(conn: psycopg.Connection, md5: str, key: str) -> int:
    """Record that the item's PDF (by its MD5) is awaited, unless a PDF is
    stored for the item already; returns 1 when a record was added."""
    return conn.execute(
        "INSERT INTO pending_files (md5, item) SELECT %(md5)s, %(key)s WHERE NOT EXISTS"
        " (SELECT 1 FROM files WHERE item = %(key)s) ON CONFLICT DO NOTHING",
        {"md5": md5, "key": key}).rowcount


def seed(conn: psycopg.Connection, index_path: Path, member_name: str,
         records_path: Path | None = None, runs_dir: Path | None = None) -> dict:
    """Import the index's items for the member; returns the counts
    {"source", "created", "present", "collisions": [...], "from_records",
    "from_index", "fulltexts", "passages", "awaited_restored"}. With runs_dir, the import keeps
    a run folder with its report."""
    member = conn.execute("SELECT id FROM members WHERE name = %s AND revoked_at IS NULL",
                          (member_name,)).fetchone()
    if member is None:
        raise ValueError(f"no active member named {member_name!r}")
    run_dir = jobs.new_run(runs_dir, "seed") if runs_dir else None
    source = sqlite3.connect(f"file:{Path(index_path)}?mode=ro", uri=True)
    try:
        _check_model(conn, source)
        records = _load_records(records_path)
        fulltext = dict(source.execute("SELECT item_key, content_hash FROM papers"))
        present = {r["key"] for r in conn.execute("SELECT key FROM items")}
        counts = {"source": 0, "created": 0, "present": 0, "collisions": [],
                  "from_records": 0, "from_index": 0, "fulltexts": 0, "passages": 0,
                  "awaited_restored": 0}
        cursor = source.execute("SELECT item_key, citation_key, payload, vec FROM items"
                                " ORDER BY item_key")
        for key, citation_key, payload_text, vec in cursor:
            counts["source"] += 1
            if key in present:
                counts["present"] += 1
                if key in fulltext:
                    counts["awaited_restored"] += _await_file(conn, fulltext[key], key)
                continue
            payload = json.loads(payload_text)
            record = records.get(key)
            counts["from_records" if record else "from_index"] += 1
            record = {**(record or _record_from_index(payload)), "key": key}
            if citation_key:
                record["citationKey"] = citation_key
            if held := items.holder(conn, own_identifiers(record)):
                counts["collisions"].append(f"{key}: its identifier belongs to {held}")
                continue
            items.insert(conn, record, "donation", member["id"],
                         publisher=next(iter(payload.get("publishers") or []), None),
                         category=next(iter(payload.get("categories") or []), None),
                         venue=next(iter(payload.get("venue_folders") or []), None))
            conn.execute("UPDATE items SET embedding = %s, has_fulltext = %s WHERE key = %s",
                         (_vector(vec), key in fulltext, key))
            if key in fulltext:
                counts["fulltexts"] += 1
                counts["passages"] += _copy_passages(conn, source, key)
                _await_file(conn, fulltext[key], key)
            counts["created"] += 1
            if counts["created"] % _BATCH == 0:
                conn.commit()
        conn.commit()
    except BaseException as exc:
        conn.rollback()
        if run_dir:
            jobs.finish_run(run_dir, "failed", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        source.close()
    if run_dir:
        (run_dir / "report.md").write_text(report(counts))
        jobs.finish_run(run_dir, "partial" if counts["collisions"] else "ok",
                        summary_line(counts), counts=counts)
    return counts


def summary_line(counts: dict) -> str:
    return (f"{counts['created']} of {counts['source']} items created"
            f" ({counts['present']} already present, {len(counts['collisions'])} skipped);"
            f" {counts['fulltexts']} full texts, {counts['passages']} passages")


def report(counts: dict) -> str:
    lines = ["# Seed import", "", summary_line(counts) + ".", "",
             f"- Full records from the records file: {counts['from_records']}",
             f"- Records built from the index alone: {counts['from_index']}"]
    if counts.get("awaited_restored"):
        lines.append(f"- Awaited PDFs recorded again for items already present:"
                     f" {counts['awaited_restored']}")
    if counts["collisions"]:
        lines += ["", "## Skipped: the identifier belongs to another item", ""]
        lines += [f"- {line}" for line in counts["collisions"]]
    return "\n".join(lines) + "\n"


# --- the seed's PDFs -----------------------------------------------------------


def seed_files(conn: psycopg.Connection, folder: Path, store, runs_dir: Path | None = None) -> dict:
    """Store every PDF in `folder` (and its subfolders) that a seeded item is
    waiting for, recognised by its MD5 (pending_files), stripped like a
    donation; other files are left alone. The passages are seeded already,
    so nothing is parsed. Each file is committed on its own, so the command
    can be stopped and run again, or run on one batch of files at a time.
    A file that cannot be read as a PDF is listed and stays awaited.
    Returns {"files", "stored", "items", "already", "unknown", "unreadable": [...],
    "still_awaited"}."""
    from scriptorium_server.filestore import UnreadablePdf, digest

    run_dir = jobs.fresh_run(runs_dir, "seed-files", None) if runs_dir else None
    counts = {"files": 0, "stored": 0, "items": 0, "already": 0, "unknown": 0,
              "unreadable": [], "still_awaited": 0}
    for path in sorted(Path(folder).rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        counts["files"] += 1
        md5 = digest(path, "md5")
        awaited = conn.execute("SELECT p.item, i.provenance_member FROM pending_files p"
                               " JOIN items i ON i.key = p.item WHERE p.md5 = %s ORDER BY p.item",
                               (md5,)).fetchall()
        if not awaited:
            held = conn.execute("SELECT 1 FROM files WHERE source_md5 = %s", (md5,)).fetchone()
            counts["already" if held else "unknown"] += 1
            continue
        try:
            sha256, size = store.put(path)
        except UnreadablePdf as exc:
            counts["unreadable"].append(f"{path.name}: {exc}")
            continue
        for row in awaited:
            conn.execute("INSERT INTO files (sha256, item, bytes, donated_by, source_md5)"
                         " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                         (sha256, row["item"], size, row["provenance_member"], md5))
            conn.execute("DELETE FROM pending_files WHERE md5 = %s AND item = %s",
                         (md5, row["item"]))
        conn.commit()
        counts["stored"] += 1
        counts["items"] += len(awaited)
    counts["still_awaited"] = conn.execute("SELECT count(*) AS n FROM pending_files").fetchone()["n"]
    if run_dir:
        (run_dir / "report.md").write_text(files_report(counts))
        jobs.finish_run(run_dir, "partial" if counts["unreadable"] else "ok",
                        files_summary(counts), counts=counts)
    return counts


def files_summary(counts: dict) -> str:
    return (f"{counts['stored']} of {counts['files']} PDFs stored for {counts['items']} items"
            f" ({counts['already']} stored before, {counts['unknown']} not awaited,"
            f" {len(counts['unreadable'])} unreadable); {counts['still_awaited']} PDFs still awaited")


def files_report(counts: dict) -> str:
    lines = ["# Seed PDFs", "", files_summary(counts) + "."]
    if counts["unreadable"]:
        lines += ["", "## Not readable as a PDF (still awaited)", ""]
        lines += [f"- {line}" for line in counts["unreadable"]]
    return "\n".join(lines) + "\n"
