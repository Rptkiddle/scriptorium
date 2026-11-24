"""The admin command (`scriptorium`): members and keys, subscribed venues,
jobs, settings, the database schema, the worker, the seed import and
passage cleaning."""

import collections
import os
from pathlib import Path
from typing import Annotated

import psycopg
import typer

from scriptorium_server import __version__, db, embedding, jobs, members, schedule, settings
from scriptorium_server import seed as seeding
from scriptorium_server import venues as subscriptions

app = typer.Typer(no_args_is_help=True, help="Administer a Scriptorium server.")
members_app = typer.Typer(no_args_is_help=True, help="Members and their API keys.")
venues_app = typer.Typer(no_args_is_help=True, help="The venues new items are harvested from.")
jobs_app = typer.Typer(no_args_is_help=True, help="Background jobs and their failures.")
settings_app = typer.Typer(no_args_is_help=True, help="The server's settings.")
passages_app = typer.Typer(no_args_is_help=True, help="The passages of full texts.")
app.add_typer(members_app, name="members")
app.add_typer(venues_app, name="venues")
app.add_typer(jobs_app, name="jobs")
app.add_typer(settings_app, name="settings")
app.add_typer(passages_app, name="passages")

ADAPTERS = subscriptions.ADAPTERS
SCHEDULE_EVERY = 60.0  # seconds between the worker's looks at the weekly schedule
# Handlers added to (or replacing) the standard ones of job_handlers().
HANDLERS: dict[str, jobs.Handler] = {}
# Whether the worker embeds waiting items and passages when idle (off in tests).
# How many texts the idle worker embeds before it looks at the job queue
# and the settings again.
IDLE_PORTION = 2000


def _data(current: settings.Settings) -> Path:
    return Path(str(current["data.folder"]))


def job_handlers(current: settings.Settings) -> dict[str, jobs.Handler]:
    """The handler for each kind of job the worker runs. Nothing heavy
    happens here: GROBID is asked and the model loaded at the first job."""
    from scriptorium_server import fulltext
    from scriptorium_server.sources import apis

    sources = current.section("sources")
    apis.configure(str(sources["contact_email"]), str(sources["s2_api_key"]))
    context: list[fulltext.Context] = []

    def run_fulltext(conn, job):
        return fulltext.process(conn, job, worker_context(conn, current, context))

    def run_backfill(conn, job):
        from scriptorium_server import intake

        venue = conn.execute("SELECT * FROM venues WHERE id = %s",
                             (job.payload["venue_id"],)).fetchone()
        if venue is None:
            raise ValueError(f"no venue with id {job.payload['venue_id']}")
        result = intake.backfill(conn, venue, job.run_dir,
                                 api_key=str(sources["openalex_api_key"]),
                                 since=job.payload.get("since") or intake.DEFAULT_SINCE)
        return intake.summary(result)

    def run_surveillance(conn, job):
        """The weekly harvest, or one an admin started. The settings are
        read when it starts, so a change needs no restart."""
        from scriptorium_server import surveillance

        now = settings.load(current.path)
        try:
            result = harvest(conn, now, job.run_dir, dry_run=bool(job.payload.get("dry_run")),
                             days=job.payload.get("days"))
        except Exception as exc:  # noqa: BLE001 — the message may hold the key
            conn.rollback()
            raise RuntimeError(f"surveillance stopped ({type(exc).__name__})") from None
        return surveillance.summary(result)

    def run_backup(conn, job):
        from scriptorium_server import backup as backups

        now = settings.load(current.path)
        result = backups.backup(conn, _data(now), Path(str(now["backup.folder"])),
                                int(now["backup.days_kept"]))
        return backups.summary_line(result)

    return {"fulltext": run_fulltext, "backfill": run_backfill,
            "surveillance": run_surveillance, "backup": run_backup}


class NoKey(RuntimeError):
    """No OpenAlex key is set, so nothing can be harvested."""


def harvest(conn, current: settings.Settings, run_dir: Path, *, dry_run: bool = False,
            days: int | None = None, parallel: int | None = None,
            max_cost: float | None = None, report=print) -> dict:
    """One surveillance run over every subscribed OpenAlex venue, by the
    settings unless an input says otherwise. The key is the
    OPENALEX_API_KEY environment variable, else the setting."""
    import os

    from scriptorium_server import surveillance
    from scriptorium_server.sources import apis

    sources = current.section("sources")
    key = os.environ.get("OPENALEX_API_KEY") or str(sources["openalex_api_key"])
    if not key:
        raise NoKey("no OpenAlex key: set OPENALEX_API_KEY or sources.openalex_api_key")
    apis.configure(str(sources["contact_email"]), str(sources["s2_api_key"]))
    venues = conn.execute("SELECT * FROM venues WHERE subscribed AND adapter = 'openalex'"
                          " ORDER BY id").fetchall()
    return surveillance.surveil(
        conn, venues, run_dir, days=days or int(current["surveillance.window_days"]),
        api_key=key, sources=sources, dry_run=dry_run,
        parallel=parallel or int(current["surveillance.parallel"]),
        max_cost=max_cost if max_cost is not None else float(current["surveillance.max_cost"]),
        report=report)


def worker_context(conn, current: settings.Settings, made: list):
    """The full-text job's context, made once (the embedder must be the
    collection's model revision, which the database records)."""
    from scriptorium_server import fulltext
    from scriptorium_server.embedding import RealEmbedder
    from scriptorium_server.filestore import FileStore
    from scriptorium_server.grobid import Grobid
    from scriptorium_server.sources import crossref
    from scriptorium_server.tei import TeiStore

    if not made:
        made.append(fulltext.Context(
            grobid=Grobid(str(current["grobid.address"])), tei=TeiStore(_data(current) / "tei"),
            files=FileStore(_data(current)),
            embedder=RealEmbedder(revision=embedding.stored_revision(conn) or None),
            registry=crossref.fetch, batch_size=int(current["embedding.batch_size"])))
    return made[0]


def connect() -> psycopg.Connection:
    return db.connect()


def _fail(message: str) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def _when(moment) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment else "-"


@app.callback()
def main() -> None:
    """Administer a Scriptorium server."""


@app.command()
def version() -> None:
    """Print the server's version."""
    typer.echo(__version__)


@app.command()
def migrate() -> None:
    """Bring the database schema up to date."""
    with connect() as conn:
        applied = db.migrate(conn)
    typer.echo("Applied: " + ", ".join(applied) if applied else "The schema is up to date.")


@app.command()
def worker(poll_seconds: Annotated[float, typer.Option(help="Wait between looks at an empty queue.")] = 5.0,
           until_idle: Annotated[bool, typer.Option(help="Stop when nothing waits.")] = False) -> None:
    """Run waiting jobs, one at a time, until stopped. The worker also
    keeps the weekly schedule: at the day and hour the settings name it
    queues the harvest and, behind it, the backup."""
    import time

    current = settings.load()
    handlers = {**job_handlers(current), **HANDLERS}
    made: list = []
    looked = [0.0]

    def scheduled(conn) -> None:
        if time.monotonic() - looked[0] < SCHEDULE_EVERY:
            return
        looked[0] = time.monotonic()
        if queued := schedule.due(conn, settings.load()):
            typer.echo(f"Queued the scheduled harvest and backup (jobs {queued}).")

    def idle(conn) -> bool:
        """Items and passages still without a vector get one, a portion at
        a time; true when a portion was embedded. The settings are read on
        each call, so embedding.when_idle and embedding.batch_size take
        effect without a restart."""
        now = settings.load()
        if now["embedding.when_idle"] != "on":
            return False
        if not any(embedding.pending(conn).values()):
            return False
        return any(embedding.embed_pending(
            conn, worker_context(conn, current, made).embedder,
            int(now["embedding.batch_size"]), limit=IDLE_PORTION))

    with connect() as conn:
        ran = jobs.run_worker(conn, _data(current) / "runs", handlers, poll_seconds=poll_seconds,
                              until_idle=until_idle, idle=idle,
                              before=None if until_idle else scheduled)
    typer.echo(f"Ran {ran} job(s).")


@app.command()
def serve(host: Annotated[str, typer.Option(help="The address to listen on.")] = "127.0.0.1",
          port: Annotated[int, typer.Option(help="The port to listen on.")] = 8765) -> None:
    """Run the web service that members' clients talk to."""
    import uvicorn
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from scriptorium_server.api import create_app
    from scriptorium_server.embedding import RealEmbedder
    from scriptorium_server.sources import apis, crossref

    current = settings.load()
    sources = current.section("sources")
    apis.configure(str(sources["contact_email"]), str(sources["s2_api_key"]))
    pool = ConnectionPool(str(current["database.address"]), kwargs={"row_factory": dict_row},
                          min_size=1, max_size=10, open=True)
    with pool.connection() as conn:
        db.migrate(conn)
        revision = embedding.stored_revision(conn)
    embedder = RealEmbedder(revision=revision)
    try:
        embedder.embed_query("ready")  # load now, so a missing model stops the start
    except (RuntimeError, OSError) as exc:
        _fail(f"the embedding model could not be loaded ({exc}); run `scriptorium model` once")
    typer.echo(f"Embedding model revision {embedder.revision or '?'} on {embedder.device}.")
    # No access log: it would print each caller's network address, which
    # this server does not record. The usage log holds every request.
    uvicorn.run(create_app(pool.connection, embedder, current, crossref.fetch,
                           current=settings.load),
                host=host, port=port, access_log=False)


@app.command()
def model() -> None:
    """Fetch the embedding model (the revision that made the collection's
    vectors, when there are any) and check that it loads. Needs the network."""
    import os
    import time

    from scriptorium_server.embedding import RealEmbedder

    with connect() as conn:
        wanted = embedding.stored_revision(conn)
    os.environ["SCRIPTORIUM_HF_ONLINE"] = "1"
    revision = embedding.download_model(wanted)
    embedder = RealEmbedder(revision=revision)
    embedder.embed_query("warm up")
    started = time.monotonic()
    vector = embedder.embed_query("how do news recommenders affect diversity of exposure")
    typer.echo(f"Model revision {revision} loaded on {embedder.device}; a query takes"
               f" {(time.monotonic() - started) * 1000:.0f} ms and has {len(vector)} numbers.")


@app.command("seed-export")
def seed_export(
    out: Annotated[Path, typer.Argument(help="The records file to write.")],
    api: Annotated[str, typer.Option(help="Zotero's local API.")] = seeding.ZOTERO_API,
) -> None:
    """On a member's machine: write their Zotero items' full records to a
    file for `seed`. Reads only; leaves out tags, collections and notes."""
    try:
        count = seeding.export_records(out, api)
    except OSError as exc:  # includes Zotero not answering
        _fail(f"could not read Zotero at {api} (is it running?): {exc}")
    typer.echo(f"Wrote {count} records to {out}.")


@app.command()
def seed(
    index: Annotated[Path, typer.Argument(help="The personal version's index.sqlite.")],
    member: Annotated[str, typer.Option(help="The member whose library this is.")],
    records: Annotated[Path | None, typer.Option(help="The file `seed-export` wrote.")] = None,
) -> None:
    """Import a member's existing library from the personal version's index."""
    runs_dir = Path(str(settings.load()["data.folder"])) / "runs"
    with connect() as conn:
        try:
            counts = seeding.seed(conn, index, member, records, runs_dir)
        except (ValueError, OSError) as exc:
            _fail(str(exc))
    typer.echo(seeding.summary_line(counts) + ".")
    for line in counts["collisions"]:
        typer.echo(f"skipped {line}")


@app.command("seed-files")
def seed_files(
    folder: Annotated[Path, typer.Argument(help="A folder of the member's PDFs (searched with its subfolders).")],
) -> None:
    """Store the PDFs the seed import awaits (recognised by content), with
    annotations and file metadata removed. Other files are left alone.
    Can be stopped and run again."""
    from scriptorium_server.filestore import FileStore

    current = settings.load()
    if not folder.is_dir():
        _fail(f"{folder} is not a folder")
    with connect() as conn:
        counts = seeding.seed_files(conn, folder, FileStore(_data(current)), _data(current) / "runs")
    typer.echo(seeding.files_summary(counts) + ".")
    for line in counts["unreadable"]:
        typer.echo(f"unreadable: {line}")


# --- backup --------------------------------------------------------------------


@app.command()
def backup() -> None:
    """Write a backup of the database, the held PDFs and the parsed full
    texts to the backup folder, and remove backups older than the days kept."""
    from scriptorium_server import backup as backups

    current = settings.load()
    with connect() as conn:
        result = backups.backup(conn, _data(current), Path(str(current["backup.folder"])),
                                int(current["backup.days_kept"]))
    typer.echo(backups.summary_line(result) + ".")


@app.command()
def restore(
    run: Annotated[str, typer.Argument(help="The backup's name (its folder under runs/), or `latest`.")],
    database_name: Annotated[str | None, typer.Option(
        help="Restore into this database on the same database server instead of the"
             " server's own (it must exist and be empty).")] = None,
    data_folder: Annotated[Path | None, typer.Option(
        help="Restore PDFs and parsed texts into this folder instead of the data folder.")] = None,
) -> None:
    """Fill an empty database and an empty data folder from a backup."""
    from psycopg.conninfo import make_conninfo

    from scriptorium_server import backup as backups

    current = settings.load()
    try:
        found = backups.find(Path(str(current["backup.folder"])), run)
        if database_name:
            conn = db.connect(make_conninfo(str(current["database.address"]),
                                            dbname=database_name))
        else:
            conn = connect()
        with conn:
            counts = backups.restore(conn, found, data_folder or _data(current))
    except (backups.BackupError, psycopg.Error) as exc:
        _fail(str(exc))
    rows = sum(counts["tables"].values())
    typer.echo(f"Restored {found.name}: {len(counts['tables'])} tables, {rows} rows;"
               f" {counts['pdfs']} PDFs, {counts['tei']} parsed-text files.")


# --- passages ------------------------------------------------------------------


@passages_app.command("clean-stamps")
def passages_clean_stamps(
    apply: Annotated[bool, typer.Option(help="Change the passages; without it, only report.")] = False,
) -> None:
    """Remove publishers' download stamps ("Downloaded from ... by ... on
    <date>") from stored passages and make the shortened passages' vectors.
    Without --apply, report what would change. Every change is recorded in
    the run folder."""
    from scriptorium_server import cleanup
    from scriptorium_server.embedding import RealEmbedder

    current = settings.load()
    with connect() as conn:
        embedder = (RealEmbedder(revision=embedding.stored_revision(conn) or None)
                    if apply else None)
        counts = cleanup.clean_stamps(conn, _data(current) / "runs", apply, embedder,
                                      int(current["embedding.batch_size"]))
    typer.echo(cleanup.summary_line(counts, apply) + f". Report: {counts['run']}/report.md")


# --- members -------------------------------------------------------------------


@members_app.command("add")
def members_add(name: str) -> None:
    """Add a member and print their API key, once."""
    with connect() as conn:
        try:
            key = members.add(conn, name)
        except ValueError as exc:
            _fail(str(exc))
    typer.echo(f"Member {name.strip()!r} added. Their API key, shown this once and not stored:")
    typer.echo(key)


@members_app.command("list")
def members_list() -> None:
    """List members."""
    with connect() as conn:
        rows = members.listing(conn)
    if not rows:
        typer.echo("No members.")
    for row in rows:
        state = f"revoked {_when(row['revoked_at'])}" if row["revoked_at"] else "active"
        typer.echo(f"{row['id']}\t{row['name']}\tadded {_when(row['created_at'])}\t{state}"
                   + ("\tadmin" if row["admin"] else ""))


@members_app.command("admin")
def members_admin(
    name: str,
    remove: Annotated[bool, typer.Option("--remove", help="Make them a plain member again.")] = False,
) -> None:
    """Make a member an admin: their client then shows the admin menu
    (after it restarts). Only this command gives or takes the mark."""
    with connect() as conn:
        found = members.set_admin(conn, name, not remove)
    if not found:
        _fail(f"no active member named {name.strip()!r}")
    typer.echo(f"Member {name.strip()!r} is " + ("no longer an admin." if remove else "an admin."))


@members_app.command("revoke")
def members_revoke(name: str) -> None:
    """Refuse a member's API key from now on."""
    with connect() as conn:
        found = members.revoke(conn, name)
    if not found:
        _fail(f"no active member named {name.strip()!r}")
    typer.echo(f"Member {name.strip()!r} revoked.")


# --- venues --------------------------------------------------------------------


@venues_app.command("add")
def venues_add(
    name: str,
    adapter: Annotated[str, typer.Option(help="The source that harvests it: crossref or openalex.")],
    issn: Annotated[list[str] | None, typer.Option(help="An ISSN; repeat for several.")] = None,
    source_id: Annotated[str | None, typer.Option(help="The venue's identifier at the source.")] = None,
    publisher: Annotated[str | None, typer.Option()] = None,
    category: Annotated[str | None, typer.Option(help="Journals, Proceedings, Preprints, ...")] = None,
    needs_filter: Annotated[bool, typer.Option(help="Too broad to take whole: only its social-science papers are admitted.")] = False,
) -> None:
    """Subscribe to a venue (adds it, or updates one already known)."""
    with connect() as conn:
        try:
            subscriptions.subscribe(conn, name, adapter, issns=issn, source_id=source_id,
                                    publisher=publisher, category=category,
                                    needs_filter=needs_filter)
        except ValueError as exc:
            _fail(str(exc) if "adapter" not in str(exc)
                  else f"--adapter must be one of {', '.join(ADAPTERS)}")
    typer.echo(f"Subscribed to {name.strip()!r}.")


@venues_app.command("list")
def venues_list() -> None:
    """List subscribed venues."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM venues WHERE subscribed ORDER BY lower(name)").fetchall()
    if not rows:
        typer.echo("No subscribed venues.")
    for row in rows:
        marker = f"harvested to {_when(row['marker'])}" if row["marker"] else "not yet harvested"
        waiting = "\tsocial-science papers only" if row["needs_filter"] else ""
        typer.echo(f"{row['name']}\t{row['adapter']}\t{', '.join(row['issns']) or '-'}"
                   f"\t{marker}{waiting}")


@venues_app.command("remove")
def venues_remove(name: str) -> None:
    """Stop harvesting a venue. Its items stay in the collection."""
    with connect() as conn:
        row = subscriptions.unsubscribe(conn, name)
    if row is None:
        _fail(f"no subscribed venue named {name.strip()!r}")
    typer.echo(f"Unsubscribed from {name.strip()!r}; its items stay.")


@venues_app.command("import")
def venues_import(
    path: Annotated[Path, typer.Argument(help="A journal list: CSV with the columns openalex_id,"
                                              " journal, issns, publisher and coded_class.")],
) -> None:
    """Subscribe to every journal in a list, harvested through OpenAlex.
    Journals of class MIXED are too broad to take whole: only their papers
    in OpenAlex's Social Sciences domain are admitted. A journal whose
    name another journal holds already is named with its publisher added."""
    import csv

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    added = renamed = 0
    with connect() as conn:
        for row in rows:
            source_id, name = row["openalex_id"].strip(), row["journal"].strip()
            issns = [i.strip() for i in (row.get("issns") or "").split(";") if i.strip()]
            other = conn.execute("SELECT source_id FROM venues WHERE lower(name) = lower(%s)",
                                 (name,)).fetchone()
            if other and other["source_id"] not in (None, source_id):
                name, renamed = f"{name} ({row.get('publisher') or source_id})", renamed + 1
            conn.execute(
                "INSERT INTO venues (name, issns, adapter, source_id, publisher, category,"
                " subscribed, needs_filter) VALUES (%s, %s, 'openalex', %s, %s, 'Journals',"
                " true, %s) ON CONFLICT (lower(name)) DO UPDATE SET issns = EXCLUDED.issns,"
                " adapter = 'openalex', source_id = EXCLUDED.source_id,"
                " publisher = coalesce(venues.publisher, EXCLUDED.publisher),"
                " category = coalesce(venues.category, EXCLUDED.category),"
                " subscribed = true, needs_filter = EXCLUDED.needs_filter",
                (name, issns, source_id, row.get("publisher") or None,
                 (row.get("coded_class") or "").strip().upper() == "MIXED"))
            added += 1
    typer.echo(f"Subscribed to {added} journals ({renamed} named with their publisher added,"
               f" as another journal has the same name).")


@app.command()
def fetch(
    path: Annotated[Path, typer.Argument(help="A journal list: CSV with the columns"
                                              " openalex_id and journal.")],
    out: Annotated[Path, typer.Option(help="Folder for the fetched files and checklist.")],
    since: Annotated[str, typer.Option(help="First publication date, YYYY-MM-DD.")] = "2020-01-01",
    parallel: Annotated[int, typer.Option(help="Journals fetched at once.")] = 8,
    max_cost: Annotated[float, typer.Option(help="Stop starting journals at this cost (USD).")] = 4.0,
) -> None:
    """Fetch every listed journal's works from OpenAlex to files, without
    touching the database: one compressed file per journal, as OpenAlex
    returns them, and a checklist (checklist.jsonl). The key is the
    OPENALEX_API_KEY environment variable, else the setting. Run again,
    it fetches only the journals not yet complete."""
    import csv
    import os

    from scriptorium_server import intake

    key = os.environ.get("OPENALEX_API_KEY") or str(settings.load()["sources.openalex_api_key"])
    if not key:
        _fail("no OpenAlex key: set OPENALEX_API_KEY or sources.openalex_api_key")
    journals = [{"source_id": r["openalex_id"].strip(), "journal": r.get("journal", "").strip()}
                for r in csv.DictReader(path.open(encoding="utf-8"))]
    rows = intake.fetch_list(journals, out, since=since, api_key=key, parallel=parallel,
                             max_cost=max_cost, report=typer.echo)
    states = collections.Counter(r["status"] for r in rows)
    cost = sum(float(r.get("cost_usd") or 0) for r in rows)
    typer.echo(f"Done: {dict(states)}; {sum(r.get('fetched', 0) for r in rows)} works;"
               f" ${cost:.2f}; files in {out}")


def _openalex_venues(conn, source_id: list[str] | None, all_venues: bool) -> list[dict]:
    """The venues named by their OpenAlex source IDs, or every subscribed
    OpenAlex venue."""
    if bool(source_id) == all_venues:
        _fail("name venues with --source-id, or use --all")
    if all_venues:
        return conn.execute("SELECT * FROM venues WHERE subscribed AND adapter = 'openalex'"
                            " ORDER BY id").fetchall()
    venues = conn.execute("SELECT * FROM venues WHERE source_id = ANY(%s)"
                          " AND adapter = 'openalex' ORDER BY id", (source_id,)).fetchall()
    missing = set(source_id) - {v["source_id"] for v in venues}
    if missing:
        _fail(f"no OpenAlex venue with source ID {', '.join(sorted(missing))}")
    return venues


@app.command()
def load(
    files: Annotated[Path, typer.Argument(help="The folder `fetch` wrote the journals' files to.")],
    source_id: Annotated[list[str] | None, typer.Option(help="A venue's OpenAlex source ID;"
                                                       " repeat for several.")] = None,
    all_venues: Annotated[bool, typer.Option("--all", help="Every subscribed OpenAlex"
                                             " venue.")] = False,
    abstracts: Annotated[list[Path] | None, typer.Option(
        help="A file of {\"doi\", \"abstract\"} lines (may be .gz) with abstracts for papers"
             " OpenAlex has none for; repeat for several.")] = None,
    labels: Annotated[list[Path] | None, typer.Option(
        help="A file of {\"id\", \"domain\"} lines (may be .gz) with the OpenAlex domain of"
             " works that were fetched without their topic; repeat for several.")] = None,
) -> None:
    """Load venues' fetched files into the collection, in bulk: every work
    that passes the harvest rules becomes an item, unless the collection
    holds it. Items get their vectors from the worker afterwards. Run
    again, it adds only what is new."""
    from scriptorium_server import load as loading

    current = settings.load()
    with connect() as conn:
        venues = _openalex_venues(conn, source_id, all_venues)
        found = loading.read_abstracts(abstracts or [])
        run_dir = jobs.new_run(_data(current) / "runs", "load")
        result = loading.load_files(conn, venues, files, run_dir, abstracts=found,
                                    domains=loading.read_domains(labels or []),
                                    report=typer.echo)
    line = (f"{len(venues)} venues in {result['seconds']:.0f} s: " + intake_summary(result))
    jobs.finish_run(run_dir, "partial" if result.get("failed") or result.get("no file")
                    else "ok", line)
    typer.echo(line + f"; counts in {run_dir}")


def intake_summary(result: dict) -> str:
    from scriptorium_server import intake

    extra = "".join(f", {result[name]} {name}" for name in ("repeated in the files", "failed",
                                                             "no file") if result.get(name))
    return intake.summary(result) + extra


@app.command()
def backfill(
    source_id: Annotated[list[str] | None, typer.Option(help="A venue's OpenAlex source ID;"
                                                       " repeat for several.")] = None,
    all_venues: Annotated[bool, typer.Option("--all", help="Every subscribed OpenAlex"
                                             " venue.")] = False,
    since: Annotated[str, typer.Option(help="First publication date, YYYY-MM-DD.")] = "2020-01-01",
) -> None:
    """Queue a harvest of venues' papers since a date, one job per venue,
    for the worker. Run again, it adds only what is new."""
    with connect() as conn:
        venues = _openalex_venues(conn, source_id, all_venues)
        for venue in venues:
            jobs.enqueue(conn, "backfill", {"venue_id": venue["id"], "since": since})
    typer.echo(f"Queued {len(venues)} harvest jobs; the worker runs them in turn.")


@app.command()
def surveil(
    days: Annotated[int | None, typer.Option(help="Days back, by publication date; by default"
                                                  " the setting surveillance.window_days.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Fetch and count; write nothing"
                                          " to the collection.")] = False,
    parallel: Annotated[int | None, typer.Option(help="Requests to OpenAlex at once; by default"
                                                      " the setting surveillance.parallel.")] = None,
    max_cost: Annotated[float | None, typer.Option(help="Stop asking at this cost (USD); by"
                                                        " default the setting surveillance.max_cost.")] = None,
) -> None:
    """Harvest the subscribed venues' recent papers: every work published
    in the last days that passes the harvest rules becomes an item, unless
    the collection holds it; recent items without an abstract are looked
    up again. Items get their vectors from the worker afterwards. The
    worker runs this every week by the settings surveillance.day and
    surveillance.hour; this command runs it by hand, at once. Run again,
    it adds only what is new. The key is the OPENALEX_API_KEY environment
    variable, else the setting."""
    from scriptorium_server import surveillance

    current = settings.load()
    if not (os.environ.get("OPENALEX_API_KEY") or current["sources.openalex_api_key"]):
        _fail("no OpenAlex key: set OPENALEX_API_KEY or sources.openalex_api_key")
    with connect() as conn:
        run_dir = jobs.new_run(_data(current) / "runs", "surveillance",
                               "dry-run" if dry_run else None)
        try:
            result = harvest(conn, current, run_dir, dry_run=dry_run, days=days,
                             parallel=parallel, max_cost=max_cost, report=typer.echo)
        except Exception as exc:  # noqa: BLE001 — the message may hold the key
            jobs.finish_run(run_dir, "failed", f"stopped: {type(exc).__name__}")
            _fail(f"surveillance stopped ({type(exc).__name__}); run folder {run_dir}")
    line = surveillance.summary(result)
    jobs.finish_run(run_dir, "ok" if surveillance.complete(result) else "partial", line)
    typer.echo(f"{run_dir.name} {line}")


# --- jobs ----------------------------------------------------------------------


@jobs_app.command("list")
def jobs_list(
    status: Annotated[str | None, typer.Option(help="waiting, running, done or failed.")] = None,
    limit: Annotated[int, typer.Option(help="How many, newest first.")] = 20,
) -> None:
    """List jobs, newest first, with the error of each failed one."""
    with connect() as conn:
        counts = conn.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status"
                              " ORDER BY status").fetchall()
        rows = conn.execute(
            "SELECT id, kind, status, created_at, error FROM jobs"
            " WHERE %(status)s::text IS NULL OR status = %(status)s ORDER BY id DESC"
            " LIMIT %(limit)s", {"status": status, "limit": limit}).fetchall()
    typer.echo(", ".join(f"{c['n']} {c['status']}" for c in counts) or "No jobs.")
    for row in rows:
        error = f"\t{row['error'].splitlines()[0]}" if row["error"] else ""
        typer.echo(f"{row['id']}\t{row['kind']}\t{row['status']}\t{_when(row['created_at'])}{error}")


@jobs_app.command("retry")
def jobs_retry(job_id: int) -> None:
    """Run a failed job again (e.g. one that failed because GROBID or a
    registry could not be reached). An upload rejected for good is not
    run again: its file is gone, and its reason stays for the member."""
    with connect() as conn:
        row = conn.execute("SELECT error FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row and (row["error"] or "").startswith("Rejected:"):
            _fail(f"job {job_id} was rejected for good ({row['error']}); its file is gone")
        if not jobs.retry(conn, job_id):
            _fail(f"no failed job {job_id}")
    typer.echo(f"Job {job_id} is waiting again.")


@jobs_app.command("show")
def jobs_show(job_id: int) -> None:
    """Show one job in full."""
    with connect() as conn:
        row = conn.execute(
            "SELECT jobs.*, members.name AS member_name FROM jobs"
            " LEFT JOIN members ON members.id = jobs.member WHERE jobs.id = %s",
            (job_id,)).fetchone()
    if row is None:
        _fail(f"no job {job_id}")
    typer.echo(f"job {row['id']}: {row['kind']}, {row['status']}")
    typer.echo(f"member:   {row['member_name'] or '-'}")
    typer.echo(f"created:  {_when(row['created_at'])}")
    typer.echo(f"started:  {_when(row['started_at'])}")
    typer.echo(f"finished: {_when(row['finished_at'])}")
    typer.echo(f"run:      {row['run_dir'] or '-'}")
    typer.echo(f"payload:  {row['payload']}")
    if row["error"]:
        typer.echo(f"error:    {row['error']}")


# --- settings ------------------------------------------------------------------


@settings_app.command("show")
def settings_show() -> None:
    """Show every setting with its value and what it does."""
    try:
        rows = settings.show()
    except settings.SettingsError as exc:
        _fail(str(exc))
    typer.echo(f"Settings file: {settings.settings_path()}")
    for name, value, explanation in rows:
        typer.echo(f"\n{name} = {value}\n    {explanation}")


@settings_app.command("set")
def settings_set(name: str, value: str) -> None:
    """Change one setting. `settings show` and the admin menu say when
    each setting takes effect."""
    try:
        settings.set_value(name, value)
    except settings.SettingsError as exc:
        _fail(str(exc))
    shown = dict((n, v) for n, v, _ in settings.show())[name]
    typer.echo(f"{name} = {shown}")
