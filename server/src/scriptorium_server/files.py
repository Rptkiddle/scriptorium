"""Files on the server side: receiving a donated PDF, serving a held PDF,
the list of what is held, and a member's donation progress.

A donation is received in one request and processed later: the PDF goes
to the holding area under a name the server makes, a full-text job
(fulltext.process) is queued, and the reply comes at once. The member
follows it with donation progress, which shows only their own donations.

"Held" means a PDF stored on the server (table `files`). An item can have
full text without a held PDF (the seed import brings passages before
their PDFs).
"""

import uuid
from pathlib import Path

import psycopg

from scriptorium_server import jobs
from scriptorium_server.filestore import FileStore
from scriptorium_server.identity import classify

PDF_MAGIC = b"%PDF-"


class NotHeld(LookupError):
    """No PDF is held for the item asked for."""


def receive(conn: psycopg.Connection, data: bytes, member: int, holding: Path,
            identifier: str | None = None, item: str | None = None) -> dict:
    """Put a donated PDF in the holding area and queue its full-text job;
    returns {"donation": job id, "status": "waiting"}. ValueError for a file
    that is not a PDF or an identifier that is not a DOI or arXiv ID."""
    if not data.startswith(PDF_MAGIC):
        raise ValueError("the file is not a PDF")
    payload: dict = {}
    if item:
        payload["item"] = item
    elif identifier:
        kind, value = classify(identifier)
        if kind not in ("doi", "arxiv"):
            raise ValueError(f"{identifier!r} is not a DOI or arXiv ID")
        payload["identifier"] = f"arxiv:{value}" if kind == "arxiv" else value
    holding.mkdir(parents=True, exist_ok=True)
    path = holding / f"{uuid.uuid4().hex}.pdf"
    path.write_bytes(data)
    payload["file"] = str(path)
    job_id = jobs.enqueue(conn, "fulltext", payload, member)
    return {"donation": job_id, "status": "waiting"}


def held(conn: psycopg.Connection) -> dict:
    """Every item with a held PDF: key, DOI, arXiv ID and the MD5 of each PDF
    as donated, so a client can tell locally what it need not send."""
    rows = conn.execute(
        "SELECT i.key, i.doi, i.arxiv_id, array_agg(DISTINCT f.source_md5)"
        " FILTER (WHERE f.source_md5 IS NOT NULL) AS md5s FROM files f"
        " JOIN items i ON i.key = f.item GROUP BY i.key, i.doi, i.arxiv_id ORDER BY i.key"
    ).fetchall()
    return {"count": len(rows), "items": [
        {"key": r["key"], "doi": r["doi"], "arxiv_id": r["arxiv_id"], "md5": r["md5s"] or []}
        for r in rows]}


def held_file(conn: psycopg.Connection, key: str, store: FileStore) -> dict:
    """The held PDF of an item (key or citation key): {"key", "citation_key",
    "path", "sha256", "bytes"}. NotHeld when there is none."""
    row = conn.execute(
        "SELECT i.key, i.citation_key, i.has_fulltext, f.sha256, f.bytes FROM items i"
        " LEFT JOIN files f ON f.item = i.key WHERE i.key = %(k)s OR i.citation_key = %(k)s"
        " ORDER BY f.donated_at LIMIT 1", {"k": key}).fetchone()
    if row is None:
        raise NotHeld(f"no item with key or citation key {key!r}")
    if row["sha256"] is None:
        raise NotHeld(f"no PDF is held for {key}" + (
            " (its full text is held; the PDF itself has not been donated yet)"
            if row["has_fulltext"] else ""))
    path = store.path(row["sha256"])
    if not path.exists():
        raise NotHeld(f"the PDF of {key} is recorded but missing from the file store;"
                      " please tell the server's administrator")
    return {"key": row["key"], "citation_key": row["citation_key"], "path": path,
            "sha256": row["sha256"], "bytes": row["bytes"]}


def _state(row: dict) -> tuple[str, str | None]:
    """A donation's state as the member sees it, and its result."""
    if row["status"] in ("waiting", "running"):
        return ("waiting" if row["status"] == "waiting" else "processing"), None
    if row["status"] == "failed":
        error = row["error"] or ""
        if error.startswith("Rejected:"):
            return "rejected", error.removeprefix("Rejected:").strip()
        return "failed", ("the server could not process it this time (for instance, its PDF"
                          " reader was not answering); the server's administrator can run it"
                          " again")
    summary = row["summary"] or ""
    state = summary.split(":", 1)[0] if ":" in summary else "done"
    return (state if state in ("stored", "already held") else "done"), summary


def donations(conn: psycopg.Connection, member: int, count: int = 50, offset: int = 0) -> dict:
    """The member's own donations, newest first: {"total", "donations": [{
    "donation", "state", "identifier", "item", "received", "finished",
    "result"}]}. States: waiting, processing, stored, already held,
    rejected, failed."""
    total = conn.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'fulltext' AND member = %s",
                         (member,)).fetchone()["n"]
    rows = conn.execute(
        "SELECT id, status, payload, error, summary, created_at, finished_at FROM jobs"
        " WHERE kind = 'fulltext' AND member = %s ORDER BY id DESC LIMIT %s OFFSET %s",
        (member, count, offset)).fetchall()
    out = []
    for r in rows:
        state, result = _state(r)
        payload = r["payload"] or {}
        out.append({"donation": r["id"], "state": state,
                    "identifier": payload.get("identifier"),
                    "item": payload.get("resolved") or payload.get("item"),
                    "received": r["created_at"].isoformat(timespec="seconds"),
                    "finished": r["finished_at"].isoformat(timespec="seconds")
                    if r["finished_at"] else None,
                    "result": result})
    return {"total": total, "donations": out}
