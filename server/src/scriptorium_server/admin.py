"""The admin command (`scriptorium`): members and keys, subscribed venues,
jobs, settings, the database schema and the worker."""

from pathlib import Path
from typing import Annotated

import psycopg
import typer

from scriptorium_server import __version__, db, jobs, members, settings
from scriptorium_server import seed as seeding

app = typer.Typer(no_args_is_help=True, help="Administer a Scriptorium server.")
members_app = typer.Typer(no_args_is_help=True, help="Members and their API keys.")
venues_app = typer.Typer(no_args_is_help=True, help="The venues new items are harvested from.")
jobs_app = typer.Typer(no_args_is_help=True, help="Background jobs and their failures.")
settings_app = typer.Typer(no_args_is_help=True, help="The server's settings.")
app.add_typer(members_app, name="members")
app.add_typer(venues_app, name="venues")
app.add_typer(jobs_app, name="jobs")
app.add_typer(settings_app, name="settings")

ADAPTERS = ("crossref", "openalex")
# The handler for each kind of job; the tasks that add a job register it here.
HANDLERS: dict[str, jobs.Handler] = {}


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
    runs_dir = Path(str(settings.load()["data.folder"])) / "runs"
    with connect() as conn:
        ran = jobs.run_worker(conn, runs_dir, HANDLERS, poll_seconds=poll_seconds,
                              until_idle=until_idle)
    typer.echo(f"Ran {ran} job(s).")


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
