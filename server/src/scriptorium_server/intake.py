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
import datetime
import gzip
import json
import time
from pathlib import Path

import psycopg

from scriptorium_server import items
from scriptorium_server.identity import creator_keys, normalise_title, own_identifiers
from scriptorium_server.sources import openalex
from scriptorium_server.sources.apis import http_session

DEFAULT_SINCE = "2020-01-01"
FETCH_FIELDS = ("source_id", "journal", "status", "openalex_count", "fetched", "unique",
                "duplicates", "requests", "cost_usd", "bytes", "seconds", "fetched_at", "problem")


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
    return admit_record(conn, record, work_id, venue)


def admit_record(conn: psycopg.Connection, record: dict, work_id: str, venue: dict) -> str:
    """admit_work for a work already in record form, placed under the
    venue's name: "new", "held" or "replaced another version"."""
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
            reason = openalex.rule(work, bool(venue.get("needs_filter")))
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
    dropped = sum(v for k, v in result.items() if k.startswith(("type:", "language:", "domain:"))
                  or k in ("retracted", "no title", "failed"))
    return (f"{result.get('new', 0)} new, {result.get('held', 0)} already held,"
            f" {result.get('replaced another version', 0)} replaced a preprint,"
            f" {result.get('harvested before', 0)} harvested before, {dropped} not admitted"
            f" (of {result.get('fetched', 0)})")


# --- fetching to files (the backfill's first step) ------------------------------


def fetch_venue(source_id: str, out_dir: Path, *, since: str = DEFAULT_SINCE,
                api_key: str = "", session=None) -> dict:
    """Save every work of one OpenAlex source published since `since`, of
    any type and as OpenAlex returns it, to <out_dir>/<source_id>.jsonl.gz
    (one work per line). The file is written under a temporary name and
    renamed when complete. Returns the source's checklist row: OpenAlex's
    count, works fetched, unique and repeated work IDs, requests, cost,
    bytes, seconds, date; status "ok" only when every work arrived once."""
    session = session or http_session("fetch")
    final = out_dir / f"{source_id}.jsonl.gz"
    part = out_dir / f"{source_id}.jsonl.gz.part"
    metas, ids, fetched = [], set(), 0
    started = time.time()
    row = {"source_id": source_id, "fetched_at": datetime.date.today().isoformat()}
    try:
        with gzip.open(part, "wt", encoding="utf-8") as f:
            for work in openalex.works(session, source_id, since, api_key, on_page=metas.append):
                fetched += 1
                ids.add(work.get("id"))
                f.write(json.dumps(work, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — the message may hold the key
        part.unlink(missing_ok=True)
        return {**row, "status": "failed", "fetched": fetched, "requests": len(metas),
                "problem": type(exc).__name__}
    count = metas[0].get("count", 0) if metas else 0
    complete = fetched == count == len(ids)
    if complete:
        part.rename(final)
    return {**row, "status": "ok" if complete else "incomplete", "openalex_count": count,
            "fetched": fetched, "unique": len(ids), "duplicates": fetched - len(ids),
            "requests": len(metas),
            "cost_usd": round(sum(float(m.get("cost_usd") or 0) for m in metas), 4),
            "bytes": (final if complete else part).stat().st_size,
            "seconds": round(time.time() - started, 1),
            "problem": "" if complete else "fetched differs from OpenAlex's count, or repeats"}


def fetch_list(journals: list[dict], out_dir: Path, *, since: str = DEFAULT_SINCE,
               api_key: str = "", parallel: int = 8, max_cost: float = 4.0,
               report=print) -> list[dict]:
    """fetch_venue for every journal ({"source_id", "journal"}), `parallel`
    at a time, skipping those whose file is complete already. Each finished
    row is appended to <out_dir>/checklist.jsonl as it comes, so a stopped
    run can be started again. Stops starting new journals once the cost so
    far reaches max_cost (US dollars)."""
    import concurrent.futures
    import threading

    out_dir.mkdir(parents=True, exist_ok=True)
    checklist = out_dir / "checklist.jsonl"
    todo = [j for j in journals if not (out_dir / f"{j['source_id']}.jsonl.gz").exists()]
    lock, spent, rows = threading.Lock(), [0.0], []

    def one(journal):
        with lock:
            if spent[0] >= max_cost:
                return None
        row = {"journal": journal.get("journal", ""),
               **fetch_venue(journal["source_id"], out_dir, since=since, api_key=api_key)}
        with lock:
            spent[0] += float(row.get("cost_usd") or 0)
            rows.append(row)
            with checklist.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            report(f"{len(rows)}/{len(todo)} {row['status']:10} {row.get('fetched', 0):7}"
                   f"  {journal.get('journal', '')[:60]}")
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        list(pool.map(one, todo))
    return rows
