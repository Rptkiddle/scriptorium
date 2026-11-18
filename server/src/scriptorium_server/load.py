"""Loading fetched files into the collection, in bulk.

`scriptorium fetch` saves each journal's works to a file; Loader admits
them. It gives the same result as admitting the works one by one
(intake.admit_work), many times faster: the identifiers, titles and keys
of the whole collection are read into memory once, and each work is
decided there.

- A work that passes the harvest rules and whose DOI and title nothing in
  the collection answers to is plainly new. Such works are written
  together, a few thousand at a time.
- A work whose DOI or title the collection may hold goes through
  intake.admit_record, the one-by-one path, which finds the held item or
  puts a published paper in its preprint's place.
- If a batch cannot be written (another session added one of its papers
  meanwhile, say), its works go through the one-by-one path too.

Abstracts found elsewhere (a file of {"doi", "abstract"} lines) fill in
where OpenAlex has none. Items are written without a vector; the worker
embeds them. A work loaded before is skipped by its OpenAlex ID, so a run
can be stopped and started again.
"""

import collections
import gzip
import json
import secrets
import time
from pathlib import Path

import psycopg

from scriptorium_server import intake, items
from scriptorium_server.citekeys import generate_citekey, unique_key
from scriptorium_server.identity import creator_keys, normalise_title, own_identifiers
from scriptorium_server.sources import openalex
from scriptorium_server.sources.abstracts import clean_abstract

BATCH = 5000
_ITEM_COLUMNS = ("key", "citation_key", "type", "title", "title_norm", "abstract", "year",
                 "date", "doi", "arxiv_id", "other_ids", "creators", "data", "venue_id",
                 "publisher", "category", "provenance_kind", "openalex_id")


def read_abstracts(paths: list[Path]) -> dict[str, str]:
    """Abstracts by DOI (lower case) from files of {"doi", "abstract"} lines."""
    found: dict[str, str] = {}
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as lines:
            for line in lines:
                if line.strip():
                    row = json.loads(line)
                    if row.get("doi") and row.get("abstract"):
                        found.setdefault(row["doi"].lower(), row["abstract"])
    return found


def read_domains(paths: list[Path]) -> dict[str, str]:
    """OpenAlex domains by work ID ("W123") from files of {"id", "domain"}
    lines: labels for works that were fetched without their topic."""
    found: dict[str, str] = {}
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as lines:
            for line in lines:
                if line.strip():
                    row = json.loads(line)
                    if row.get("id") and row.get("domain"):
                        found.setdefault(row["id"].rsplit("/", 1)[-1], row["domain"])
    return found


class Loader:
    def __init__(self, conn: psycopg.Connection, abstracts: dict[str, str] | None = None,
                 batch: int = BATCH, domains: dict[str, str] | None = None):
        self.conn = conn
        self.abstracts = abstracts or {}
        self.domains = domains or {}
        self.batch = batch
        self.keys: set[str] = set()
        self.citation_keys: set[str] = set()
        self.loaded: set[str] = set()       # OpenAlex IDs in the collection before this run
        self.seen: set[str] = set()         # OpenAlex IDs met in this run
        self.held_ids: set[str] = set()     # identifiers the collection answers to
        self.held_titles: set[str] = set()
        self.new_ids: set[str] = set()      # identifiers of works admitted in this run
        self.new_without_doi: dict[str, list[set[str]]] = collections.defaultdict(list)
        self._waiting: list[tuple[dict, list, str, dict, collections.Counter]] = []
        with conn.cursor(name="loader_state") as rows:
            rows.execute("SELECT key, citation_key, openalex_id, doi, arxiv_id, other_ids,"
                         " title_norm FROM items")
            for row in rows:
                self.keys.add(row["key"])
                if row["citation_key"]:
                    self.citation_keys.add(row["citation_key"])
                if row["openalex_id"]:
                    self.loaded.add(row["openalex_id"])
                if row["doi"]:
                    self.held_ids.add("doi:" + row["doi"].lower())
                if row["arxiv_id"]:
                    self.held_ids.add("arxiv:" + row["arxiv_id"].lower())
                self.held_ids.update(i.lower() for i in row["other_ids"] or [])
                self.held_titles.add(row["title_norm"])
        conn.commit()

    # --- one venue ----------------------------------------------------------------

    def load_venue(self, venue: dict, path: Path) -> dict:
        """Admit every work in the venue's file that passes the harvest
        rules; returns the counts: works read, each outcome, each rule's drops."""
        counts: collections.Counter = collections.Counter()
        started = time.time()
        social_only = bool(venue.get("needs_filter"))
        with gzip.open(path, "rt", encoding="utf-8") as lines:
            for line in lines:
                if not line.strip():
                    continue
                work = json.loads(line)
                counts["fetched"] += 1
                if social_only and not work.get("primary_topic"):  # fetched without its topic
                    label = self.domains.get((work.get("id") or "").rsplit("/", 1)[-1])
                    if label:
                        work["primary_topic"] = {"domain": {"display_name": label}}
                counts[openalex.rule(work, social_only) or self._admit(work, venue, counts)] += 1
        self.flush()
        counts.pop(None, None)
        return {"venue": venue["name"], "source_id": venue["source_id"],
                "seconds": round(time.time() - started, 1), **dict(counts)}

    def _admit(self, work: dict, venue: dict, counts: collections.Counter) -> str | None:
        """What happened to a work that passed the rules. None for a work
        waiting to be written with its batch: it is counted when written."""
        work_id = (work.get("id") or "").rsplit("/", 1)[-1]
        if work_id in self.seen:
            return "repeated in the files"
        self.seen.add(work_id)
        if work_id in self.loaded:
            return "harvested before"
        record = openalex.to_record(work)
        record["publicationTitle"] = venue["name"]
        title = normalise_title(record.get("title", ""))
        if not title:
            return "no title"
        if not record.get("abstractNote") and record.get("DOI"):
            if abstract := clean_abstract(self.abstracts.get(record["DOI"].lower(), "")):
                record["abstractNote"] = abstract
        own = [i.lower() for i in own_identifiers(record)]
        authors = set(creator_keys(record.get("creators") or []))
        if any(i in self.new_ids for i in own):
            return "held"  # the same DOI twice in the files
        if not own and any(authors & other for other in self.new_without_doi.get(title, ())):
            return "held"  # the same paper without a DOI twice in the files
        self.new_ids.update(own)
        if not own and authors:
            self.new_without_doi[title].append(authors)
        if any(i in self.held_ids for i in own) or title in self.held_titles:
            self.flush()
            return self._one_by_one(record, work_id, venue)
        key = self._new_key()
        citation_key = unique_key(generate_citekey(record), self.citation_keys)
        self.citation_keys.add(citation_key)
        columns = items._columns(record)
        row = {**columns, "key": key, "citation_key": citation_key, "venue_id": venue["id"],
               "publisher": venue.get("publisher") or record.get("publisher"),
               "category": venue.get("category") or "Journals",
               "provenance_kind": "venue", "openalex_id": work_id or None}
        for name in ("other_ids", "creators", "data"):
            row[name] = json.dumps(row[name].obj, ensure_ascii=False)
        creators = sorted({(key, name_key, position)
                           for position, creator in enumerate(record.get("creators") or [])
                           for name_key in creator_keys([creator])})
        self._waiting.append((row, creators, work_id, record, counts))
        if len(self._waiting) >= self.batch:
            self.flush(venue)
        return None

    def _new_key(self) -> str:
        while True:
            key = "".join(secrets.choice(items._KEY_ALPHABET) for _ in range(8))
            if key not in self.keys:
                self.keys.add(key)
                return key

    def _one_by_one(self, record: dict, work_id: str, venue: dict) -> str:
        try:
            outcome = intake.admit_record(self.conn, record, work_id, venue)
        except (ValueError, psycopg.Error):
            self.conn.rollback()
            return "failed"
        if outcome == "new":  # its keys were made by the database path
            row = self.conn.execute("SELECT key, citation_key FROM items WHERE openalex_id = %s",
                                    (work_id,)).fetchone()
            self.conn.commit()
            if row:
                self.keys.add(row["key"])
                self.citation_keys.add(row["citation_key"])
        return outcome

    # --- writing ------------------------------------------------------------------

    def flush(self, venue: dict | None = None) -> None:
        """Write the waiting works in one transaction; when that fails,
        admit them one by one."""
        waiting, self._waiting = self._waiting, []
        if not waiting:
            return
        try:
            with self.conn.transaction():
                cursor = self.conn.cursor()
                with cursor.copy(f"COPY items ({', '.join(_ITEM_COLUMNS)}) FROM STDIN") as copy:
                    for row, *_ in waiting:
                        copy.write_row([row[name] for name in _ITEM_COLUMNS])
                with cursor.copy("COPY item_creators (item, name_key, position) FROM STDIN") as copy:
                    for _, creators, *_ in waiting:
                        for creator in creators:
                            copy.write_row(creator)
            self.conn.commit()
            for *_, counts in waiting:
                counts["new"] += 1
        except psycopg.Error:
            self.conn.rollback()
            for row, _, work_id, record, counts in waiting:
                venue_row = {"id": row["venue_id"], "name": record["publicationTitle"],
                             "publisher": row["publisher"], "category": row["category"]}
                counts[self._one_by_one(record, work_id, venue_row)] += 1


def load_files(conn: psycopg.Connection, venues: list[dict], files: Path, run_dir: Path, *,
               abstracts: dict[str, str] | None = None,
               domains: dict[str, str] | None = None, report=print) -> dict:
    """Load each venue's file (<files>/<source_id>.jsonl.gz). One line of
    counts per venue is appended to <run_dir>/counts.jsonl as it finishes;
    returns the totals, also written to <run_dir>/totals.json."""
    loader = Loader(conn, abstracts, domains=domains)
    totals: collections.Counter = collections.Counter()
    started = time.time()
    with (run_dir / "counts.jsonl").open("a", encoding="utf-8") as out:
        for number, venue in enumerate(venues, 1):
            path = files / f"{venue['source_id']}.jsonl.gz"
            if not path.exists():
                result = {"venue": venue["name"], "source_id": venue["source_id"], "no file": 1}
            else:
                result = loader.load_venue(venue, path)
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
            totals.update({k: v for k, v in result.items() if isinstance(v, int)})
            report(f"{number}/{len(venues)} {result.get('new', 0):7} new of"
                   f" {result.get('fetched', 0):7}  {venue['name'][:60]}")
    result = {"venues": len(venues), "seconds": round(time.time() - started, 1), **dict(totals)}
    (run_dir / "totals.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    return result
