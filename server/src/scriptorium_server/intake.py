"""The intake job: a subscribed venue's papers harvested into the collection.

backfill() takes one venue harvested through OpenAlex (adapter "openalex",
its OpenAlex source ID in source_id) and admits every work published since
a date that passes the harvest rules (sources.openalex.rule). Items are
placed under the venue's own name, publisher and category, so the venue
filter finds them whatever name OpenAlex uses. Each item records the
OpenAlex work it came from, so a run repeated later adds only what is new,
including papers without a DOI. Works already held under their DOI (from a
member's library, say) are matched, not added again.

Abstracts come with the records where OpenAlex has them; filling the rest
is a separate step. Embedding is left to the worker, which embeds waiting
items when it has nothing else to do.
"""

import collections
import json
import time
from pathlib import Path

import psycopg

from scriptorium_server import items
from scriptorium_server.identity import creator_keys, normalise_title, own_identifiers
from scriptorium_server.sources import openalex
from scriptorium_server.sources.apis import http_session

DEFAULT_SINCE = "2020-01-01"


class HarvestError(RuntimeError):
    """The source could not be read. The message names the error's class
    only: a request URL carries the API key."""


def _held_openalex(conn: psycopg.Connection, work_id: str) -> str | None:
    row = conn.execute("SELECT key FROM items WHERE openalex_id = %s", (work_id,)).fetchone()
    return row["key"] if row else None


def _same_paper_without_doi(conn: psycopg.Connection, record: dict) -> str | None:
    """For a record without a DOI: a held item with the same title, no DOI
    of its own and an author in common (the same paper from a member's
    library, say), or None."""
    keys = set(creator_keys(record.get("creators") or []))
    if not keys:
        return None
    for row in conn.execute("SELECT key, creators FROM items WHERE title_norm = %s"
                            " AND doi IS NULL AND arxiv_id IS NULL",
                            (normalise_title(record["title"]),)):
        if keys & set(creator_keys(row["creators"] or [])):
            return row["key"]
    return None


def admit_work(conn: psycopg.Connection, work: dict, venue: dict) -> str:
    """Admit one work that passed the rules; returns what happened: "new",
    "held" (an item with its DOI, or the same paper without one, was in
    the collection), "replaced another version" (a held preprint gave way
    to it) or "harvested before"."""
    work_id = (work.get("id") or "").rsplit("/", 1)[-1]
    if work_id and _held_openalex(conn, work_id):
        return "harvested before"
    record = openalex.to_record(work)
    record["publicationTitle"] = venue["name"]
    if not record.get("title"):
        raise ValueError("a work without a title")
    held = (items.holder(conn, own_identifiers(record)) if record.get("DOI")
            else _same_paper_without_doi(conn, record))
    if held:
        key, outcome = held, "held"
    else:
        key, new = items.admit(conn, record, "venue", publisher=venue.get("publisher"),
                               category=venue.get("category"))
        outcome = "new" if new else "replaced another version"
    if work_id:
        conn.execute("UPDATE items SET openalex_id = %s WHERE key = %s AND openalex_id IS NULL",
                     (work_id, key))
    conn.commit()
    return outcome


def backfill(conn: psycopg.Connection, venue: dict, run_dir: Path, *, api_key: str = "",
             since: str = DEFAULT_SINCE, session=None) -> dict:
    """Harvest one venue since `since` (YYYY-MM-DD). Returns the counts,
    also written to <run_dir>/counts.json: works fetched, each outcome and
    each rule's drops, requests, cost and seconds."""
    if venue.get("adapter") != "openalex" or not venue.get("source_id"):
        raise ValueError(f"{venue.get('name')!r} is not harvested through OpenAlex")
    session = session or http_session("intake")
    counts = collections.Counter()
    metas = []
    started = time.time()
    try:
        for work in openalex.works(session, venue["source_id"], since, api_key,
                                   on_page=metas.append):
            counts["fetched"] += 1
            reason = openalex.rule(work)
            if reason is None:
                try:
                    reason = admit_work(conn, work, venue)
                except ValueError:  # a record without a title cannot be an item
                    conn.rollback()
                    reason = "no title"
            counts[reason] += 1
    except psycopg.Error:
        raise
    except Exception as exc:  # noqa: BLE001 — the message may hold the key
        raise HarvestError(f"harvest of {venue['name']!r} stopped after {counts['fetched']}"
                           f" works: {type(exc).__name__}") from None
    result = {"venue": venue["name"], "source_id": venue["source_id"], "since": since,
              "openalex_count": metas[0].get("count") if metas else 0,
              "requests": len(metas),
              "cost_usd": round(sum(float(m.get("cost_usd") or 0) for m in metas), 4),
              "seconds": round(time.time() - started, 1), **dict(counts)}
    (run_dir / "counts.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    return result


def summary(result: dict) -> str:
    """One line for the job list."""
    dropped = sum(v for k, v in result.items() if k.startswith(("type:", "language:"))
                  or k in ("retracted", "no title"))
    return (f"{result.get('new', 0)} new, {result.get('held', 0)} already held,"
            f" {result.get('replaced another version', 0)} replaced a preprint,"
            f" {result.get('harvested before', 0)} harvested before, {dropped} not admitted"
            f" (of {result.get('fetched', 0)})")
