"""The admin command (`scriptorium`): members and keys, subscribed venues,
jobs, settings, the database schema, the worker, the seed import and
passage cleaning."""

from pathlib import Path
from typing import Annotated

import psycopg
import typer

from scriptorium_server import __version__, db, embedding, jobs, members, settings
from scriptorium_server import seed as seeding

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

ADAPTERS = ("crossref", "openalex")
# Handlers added to (or replacing) the standard ones of job_handlers().
HANDLERS: dict[str, jobs.Handler] = {}
# Whether the worker embeds waiting items and passages when idle (off in tests).
IDLE_EMBEDDING = True


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

    return {"fulltext": run_fulltext}


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
    """Run waiting jobs, one at a time, until stopped."""
    current = settings.load()
    handlers = {**job_handlers(current), **HANDLERS}
    made: list = []

    def idle(conn):  # items and passages still without a vector get one
        embedding.embed_pending(conn, worker_context(conn, current, made).embedder,
                                int(current["embedding.batch_size"]))

    with connect() as conn:
        ran = jobs.run_worker(conn, _data(current) / "runs", handlers, poll_seconds=poll_seconds,
                              until_idle=until_idle, idle=idle if IDLE_EMBEDDING else None)
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
    uvicorn.run(create_app(pool.connection, embedder, current, crossref.fetch),
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
        typer.echo(f"{row['id']}\t{row['name']}\tadded {_when(row['created_at'])}\t{state}")


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
    needs_filter: Annotated[bool, typer.Option(help="Too broad to take whole: skipped until a filter exists.")] = False,
) -> None:
    """Subscribe to a venue (adds it, or updates one already known)."""
    if adapter not in ADAPTERS:
        _fail(f"--adapter must be one of {', '.join(ADAPTERS)}")
    if not name.strip():
        _fail("a venue needs a name")
    with connect() as conn:
        conn.execute(
            "INSERT INTO venues (name, issns, adapter, source_id, publisher, category,"
            " subscribed, needs_filter) VALUES (%s, %s, %s, %s, %s, %s, true, %s)"
            " ON CONFLICT (lower(name)) DO UPDATE SET issns = EXCLUDED.issns,"
            " adapter = EXCLUDED.adapter, source_id = EXCLUDED.source_id,"
            " publisher = coalesce(EXCLUDED.publisher, venues.publisher),"
            " category = coalesce(EXCLUDED.category, venues.category),"
            " subscribed = true, needs_filter = EXCLUDED.needs_filter",
            (name.strip(), issn or [], adapter, source_id, publisher, category, needs_filter))
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
        waiting = "\tneeds filter" if row["needs_filter"] else ""
        typer.echo(f"{row['name']}\t{row['adapter']}\t{', '.join(row['issns']) or '-'}"
                   f"\t{marker}{waiting}")


@venues_app.command("remove")
def venues_remove(name: str) -> None:
    """Stop harvesting a venue. Its items stay in the collection."""
    with connect() as conn:
        row = conn.execute("UPDATE venues SET subscribed = false WHERE lower(name) = lower(%s)"
                           " AND subscribed RETURNING id", (name.strip(),)).fetchone()
    if row is None:
        _fail(f"no subscribed venue named {name.strip()!r}")
    typer.echo(f"Unsubscribed from {name.strip()!r}; its items stay.")


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
    registry could not be reached). A donation rejected for good is not
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
    """Change one setting. Takes effect when the server and worker restart."""
    try:
        settings.set_value(name, value)
    except settings.SettingsError as exc:
        _fail(str(exc))
    shown = dict((n, v) for n, v, _ in settings.show())[name]
    typer.echo(f"{name} = {shown}")
