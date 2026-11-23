"""Surveillance: the subscribed venues' recent papers, harvested on a schedule.

surveil() asks OpenAlex for every work the venues published in the last
`days` days and admits those that pass the harvest rules
(sources.openalex.rule) and that the collection does not hold. The window
is by publication date and far wider than the time between two runs:
OpenAlex lists some papers weeks after their publication date and fills in
a paper's type, language and topic later still, so each run looks at the
recent papers again. A paper harvested before is recognised by its
OpenAlex ID and costs one comparison.

To be quick, the venues are asked for together, many to a request, several
requests at a time, and each reply is settled as it arrives:

1. One query finds which of the reply's works the collection holds by
   their OpenAlex ID. A held item without an abstract takes the abstract
   OpenAlex now has for it.
2. The other works go through the harvest rules, and those that pass
   through intake.admit_record: a paper held under its DOI is matched, a
   held preprint gives way to the published paper.

Afterwards the papers of this window that still have no abstract are
looked up at Scopus, ScienceDirect and Springer Nature (sources.abstracts). An item whose
abstract arrives later loses its vector, and the worker embeds it again,
as it embeds every new item.

A group of venues that cannot be read is named in the result; the next
run's window covers it. The run stops asking once its cost passes
max_cost. Counts per venue go to <run_dir>/counts.jsonl, the totals to
<run_dir>/totals.json.
"""

import collections
import concurrent.futures
import datetime
import json
import queue
import threading
import time
from pathlib import Path

import psycopg

from scriptorium_server import intake
from scriptorium_server.sources import openalex
from scriptorium_server.sources.abstracts import (
    clean_abstract,
    fill_abstracts,
    openalex_abstract,
)
from scriptorium_server.sources.apis import http_session

GROUP = 50  # venues named in one request; smaller groups finish sooner side by side
# Where abstracts are looked up, in order. OpenAlex's own comes with the work.
ABSTRACT_SOURCES = ("scopus", "sciencedirect", "springer")
_DROPS = ("type:", "language:", "domain:")


def _fetch(groups: list[list[str]], since: str, api_key: str, parallel: int,
           out: queue.Queue, stop: threading.Event) -> None:
    """Put every reply of every group on `out` as (group number, works,
    meta); a group that cannot be read as (group number, None, the error's
    class name: its text may hold the key); None when all are done."""
    def one(number: int) -> None:
        if stop.is_set():
            return
        try:
            for works, meta in openalex.pages(http_session("surveillance"), groups[number],
                                              since, api_key):
                out.put((number, works, meta))
                if stop.is_set():
                    return
        except Exception as exc:  # noqa: BLE001 — the message may hold the key
            out.put((number, None, type(exc).__name__))

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        list(pool.map(one, range(len(groups))))
    out.put(None)


def _held(conn: psycopg.Connection, work_ids: list[str]) -> dict[str, dict]:
    """The items harvested from these OpenAlex works, by work ID."""
    rows = conn.execute("SELECT openalex_id, key, doi, abstract FROM items"
                        " WHERE openalex_id = ANY(%s)", (work_ids,)).fetchall()
    return {row["openalex_id"]: row for row in rows}


def _set_abstract(conn: psycopg.Connection, key: str, text: str) -> None:
    """Give an item without an abstract one; it is embedded again."""
    conn.execute("UPDATE items SET abstract = %s, embedding = NULL WHERE key = %s"
                 " AND coalesce(abstract, '') = ''", (text, key))


def surveil(conn: psycopg.Connection, venues: list[dict], run_dir: Path, *, days: int = 90,
            api_key: str = "", sources: dict | None = None, dry_run: bool = False,
            parallel: int = 8, max_cost: float = 1.0, today: datetime.date | None = None,
            report=print) -> dict:
    """Harvest the venues' works of the last `days` days. With dry_run
    nothing is written: works that pass the rules and are not held by
    their OpenAlex ID are counted as "would be admitted". Returns the
    totals, also written to <run_dir>/totals.json."""
    by_source = {v["source_id"]: v for v in venues
                 if v.get("adapter") == "openalex" and v.get("source_id")}
    ids = sorted(by_source)
    groups = [ids[i:i + GROUP] for i in range(0, len(ids), GROUP)]
    since = ((today or datetime.date.today()) - datetime.timedelta(days=days)).isoformat()
    per_venue: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    totals: collections.Counter = collections.Counter()
    without_abstract: dict[str, str] = {}  # item key -> DOI, for the abstract fill
    unread: list[int] = []
    requests_made, cost = 0, 0.0
    started = time.time()

    replies: queue.Queue = queue.Queue(maxsize=parallel * 2)
    stop = threading.Event()
    fetcher = threading.Thread(target=_fetch, daemon=True,
                               args=(groups, since, api_key, parallel, replies, stop))
    fetcher.start()
    try:
        while (reply := replies.get()) is not None:
            number, works, meta = reply
            if works is None:
                unread.append(number)
                report(f"group {number + 1}/{len(groups)} could not be read ({meta})")
                continue
            requests_made += 1
            cost += float(meta.get("cost_usd") or 0)
            if cost > max_cost and not stop.is_set():
                stop.set()
                report(f"cost passed ${max_cost:.2f}: no further requests")
            _settle(conn, works, by_source, per_venue, without_abstract, dry_run)
    finally:
        stop.set()
        while fetcher.is_alive():  # let the fetcher's threads finish their replies
            try:
                replies.get(timeout=0.1)
            except queue.Empty:
                pass

    for counts in per_venue.values():
        totals.update(counts)
    filled = {"missing": len(without_abstract)}
    if without_abstract and not dry_run:
        candidates = [{"key": key, "doi": doi, "abstract": ""}
                      for key, doi in without_abstract.items()]
        filled = fill_abstracts(candidates, sources or {}, sources=ABSTRACT_SOURCES,
                                max_chars=None)
        for candidate in candidates:
            if candidate["abstract"]:
                _set_abstract(conn, candidate["key"], candidate["abstract"])
        conn.commit()

    with (run_dir / "counts.jsonl").open("w", encoding="utf-8") as out:
        for source_id in ids:
            if per_venue[source_id]:
                out.write(json.dumps({"venue": by_source[source_id]["name"],
                                      "source_id": source_id, **per_venue[source_id]},
                                     ensure_ascii=False) + "\n")
    result = {"since": since, "days": days, "venues": len(ids), "dry_run": dry_run,
              "requests": requests_made, "cost_usd": round(cost, 4),
              "seconds": round(time.time() - started, 1),
              "stopped_at_cost": cost > max_cost,
              "unread_venues": [by_source[s]["name"] for n in sorted(unread)
                                for s in groups[n]],
              "abstracts": filled, **dict(totals)}
    (run_dir / "totals.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    return result


def _settle(conn: psycopg.Connection, works: list[dict], by_source: dict[str, dict],
            per_venue: dict[str, collections.Counter], without_abstract: dict[str, str],
            dry_run: bool) -> None:
    """Decide every work of one reply and count what happened under its venue."""
    work_ids = [(w.get("id") or "").rsplit("/", 1)[-1] for w in works]
    held = _held(conn, [i for i in work_ids if i])
    for work, work_id in zip(works, work_ids):
        source_id = openalex.source_of(work)
        venue = by_source.get(source_id)
        if venue is None:  # not one of the venues asked for
            continue
        counts = per_venue[source_id]
        counts["fetched"] += 1
        if item := held.get(work_id):
            counts["harvested before"] += 1
            if not (item["abstract"] or "").strip():
                text = clean_abstract(openalex_abstract(work.get("abstract_inverted_index")))
                if text:
                    counts["abstract from OpenAlex later"] += 1
                    if not dry_run:
                        _set_abstract(conn, item["key"], text)
                elif item["doi"]:
                    without_abstract[item["key"]] = item["doi"]
            continue
        reason = openalex.rule(work, bool(venue.get("needs_filter")))
        if reason is None and dry_run:
            reason = "would be admitted"
        elif reason is None:
            record = openalex.to_record(work)
            record["publicationTitle"] = venue["name"]
            try:
                reason = intake.admit_record(conn, record, work_id, venue)
            except ValueError:  # a record without a title cannot be an item
                conn.rollback()
                reason = "no title"
            except psycopg.Error:
                conn.rollback()
                reason = "failed"
            if reason == "new" and not record.get("abstractNote") and record.get("DOI"):
                row = conn.execute("SELECT key FROM items WHERE openalex_id = %s",
                                   (work_id,)).fetchone()
                if row:
                    without_abstract[row["key"]] = record["DOI"]
        counts[reason] += 1
    conn.commit()


def summary(result: dict) -> str:
    """One line for the log and the run list."""
    dropped = sum(v for k, v in result.items()
                  if k.startswith(_DROPS) or k in ("retracted", "no title", "failed"))
    abstracts = result.get("abstracts") or {}
    if result.get("dry_run"):
        line = f"dry run: {result.get('would be admitted', 0)} would be admitted"
    else:
        line = (f"{result.get('new', 0)} new, {result.get('held', 0)} already held,"
                f" {result.get('replaced another version', 0)} replaced a preprint")
    line += (f", {result.get('harvested before', 0)} harvested before, {dropped} not admitted"
             f" (of {result.get('fetched', 0)} works since {result['since']} in"
             f" {result['venues']} venues); abstracts:"
             f" {result.get('abstract from OpenAlex later', 0)} from OpenAlex later")
    if result.get("dry_run"):
        line += f", {abstracts.get('missing', 0)} to look up"
    else:
        line += "".join(f", {abstracts.get(s, 0)} from {s}" for s in ABSTRACT_SOURCES)
        line += f", {abstracts.get('title_only', 0)} still without"
        for source, why in (abstracts.get("unavailable") or {}).items():
            line += f", {source} unavailable ({why})"
    line += (f"; {result['requests']} requests, ${result['cost_usd']:.2f},"
             f" {result['seconds']:.0f} s")
    if result.get("unread_venues"):
        line += f"; {len(result['unread_venues'])} venues could not be read"
    if result.get("stopped_at_cost"):
        line += "; stopped at the cost limit"
    return line


def complete(result: dict) -> bool:
    """Whether every venue was read to the end."""
    return not (result.get("unread_venues") or result.get("stopped_at_cost")
                or result.get("failed"))
