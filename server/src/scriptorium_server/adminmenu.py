"""The admin menu: what the client's admin tools ask of the server.

Six requests, one per tool: overview, settings, members, venues, runs and
jobs. They hold what an admin does while the server runs. Installing and
repairing it, the keys for outside sources, the places of its parts and
the admin mark itself stay with the admin command on the host.

A request that would change something changes nothing the first time: it
answers with a preview (what is now, what would be, when it takes effect)
and makes the change only when sent again with confirm set. Every change
made is written to the table admin_log with its value before and after.
"""

import datetime
import os
import shutil
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from scriptorium_server import __version__, jobs, members, schedule, settings, venues
from scriptorium_server import backup as backups

RUN_KINDS = ("surveillance", "backup")
COUNT_UP_TO = 100_000  # counts of what waits for a vector stop here
CONFIRM = "Nothing was changed. Show this to the admin; on their yes, send the same request with confirm=true."


class Invalid(ValueError):
    """A request the menu cannot carry out; the message says why."""


def _iso(moment) -> str | None:
    return moment.astimezone().isoformat(timespec="minutes") if moment else None


def _preview(change: dict) -> dict:
    return {"changed": False, "preview": change, "next": CONFIRM}


def record(conn: psycopg.Connection, member: int, action: str, target: str | None,
           before=None, after=None) -> None:
    """Write one change to the admin log."""
    conn.execute("INSERT INTO admin_log (member, action, target, before, after)"
                 " VALUES (%s, %s, %s, %s, %s)",
                 (member, action, target, Jsonb(before), Jsonb(after)))


def recent_changes(conn: psycopg.Connection, count: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT a.at, coalesce(m.name, m.subject) AS by, a.action, a.target, a.before, a.after FROM admin_log a"
        " LEFT JOIN members m ON m.id = a.member ORDER BY a.id DESC LIMIT %s", (count,)).fetchall()
    return [{**r, "at": _iso(r["at"])} for r in rows]


# -- overview -------------------------------------------------------------------


def _capped(conn: psycopg.Connection, table: str, where: str) -> int | str:
    n = conn.execute(f"SELECT count(*) AS n FROM (SELECT 1 FROM {table} WHERE {where}"
                     f" LIMIT {COUNT_UP_TO + 1}) t").fetchone()["n"]
    return n if n <= COUNT_UP_TO else f"more than {COUNT_UP_TO}"


def overview(conn: psycopg.Connection, current: settings.Settings, device: str = "",
             now: datetime.datetime | None = None) -> dict:
    """The state of the server on one page."""
    now = now or datetime.datetime.now()
    size = conn.execute(
        "SELECT count(*) AS items, count(*) FILTER (WHERE has_fulltext) AS fulltexts,"
        " count(*) FILTER (WHERE added_at > now() - interval '7 days') AS added_7_days"
        " FROM items").fetchone()
    uploads = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, count(*) AS n FROM jobs WHERE kind = 'fulltext' GROUP BY status")}
    use = conn.execute(
        "SELECT count(*) FILTER (WHERE at > now() - interval '1 day') AS day,"
        " count(*) FILTER (WHERE at > now() - interval '7 days') AS week,"
        " count(DISTINCT member) FILTER (WHERE at > now() - interval '7 days') AS members"
        " FROM usage_log WHERE at > now() - interval '7 days'").fetchone()
    people = conn.execute(
        "SELECT count(*) FILTER (WHERE revoked_at IS NULL) AS active,"
        " count(*) FILTER (WHERE revoked_at IS NULL AND admin) AS admins FROM members").fetchone()
    venue_count = conn.execute("SELECT count(*) AS n FROM venues WHERE subscribed").fetchone()["n"]
    data = Path(str(current["data.folder"]))
    disk = shutil.disk_usage(data if data.is_dir() else "/")
    upcoming = schedule.moment(current, now, after=True)
    last = {kind: next(iter(_past_runs(current, kind, 1)), None) for kind in RUN_KINDS}
    return {
        "server": {"version": __version__, "commit": os.environ.get("SCRIPTORIUM_COMMIT", ""),
                   "queries_embedded_on": device or "unknown",
                   "disk_free_gb": round(disk.free / 1e9), "disk_total_gb": round(disk.total / 1e9)},
        "collection": {**size, "subscribed_venues": venue_count,
                       "items_waiting_for_a_vector": _capped(conn, "items", "embedding IS NULL"),
                       "passages_waiting_for_a_vector": _capped(
                           conn, "passages", "embedding IS NULL AND NOT artefact")},
        "pdf_uploads": {"waiting": uploads.get("waiting", 0), "running": uploads.get("running", 0),
                        "done": uploads.get("done", 0), "failed": uploads.get("failed", 0)},
        "schedule": {"next_harvest_and_backup":
                         upcoming.isoformat(timespec="minutes") if upcoming else "switched off",
                     "last_harvest": last["surveillance"], "last_backup": last["backup"],
                     "queued_or_running": _open_runs(conn)},
        "members": {**people, "active_last_7_days": use["members"]},
        "requests": {"last_24_hours": use["day"], "last_7_days": use["week"]},
        "settings_changed_from_default": {
            d["name"]: d["value"] for d in settings.describe(_path(current))
            if d["value"] != d["default"]},
        "recent_admin_changes": recent_changes(conn),
        "tools": {"admin_settings": "show or change a setting",
                  "admin_members": "members and their activity; add, revoke",
                  "admin_venues": "subscribed venues; add, remove",
                  "admin_runs": "harvests and backups; start one now",
                  "admin_jobs": "PDF upload jobs and their failures; retry"},
    }


# -- settings -------------------------------------------------------------------


def _path(current: settings.Settings) -> Path | None:
    return getattr(current, "path", None)


def settings_request(conn: psycopg.Connection, member: dict, current: settings.Settings,
                     name: str | None = None, value: str | None = None,
                     confirm: bool = False, now: datetime.datetime | None = None) -> dict:
    """Without a name every setting; with a name that setting; with a name
    and a value the change, previewed, and made when confirmed."""
    path = _path(current)
    described = {d["name"]: d for d in settings.describe(path)}
    if name is None:
        return {"settings": list(described.values()),
                "note": "Secrets (keys for outside sources) show as set or not set."}
    if name not in described:
        raise Invalid(f"unknown setting {name!r}; send no name to see them all")
    setting = described[name]
    if value is None:
        return {"setting": setting}
    if not settings.menu_may_change(name):
        raise Invalid(f"{name} is changed {settings.HOST_ONLY}")
    try:
        checked = settings.check_value(name, value)
    except settings.SettingsError as exc:
        raise Invalid(str(exc)) from exc
    change = {"setting": name, "from": setting["value"], "to": settings.shown(name, checked),
              "takes_effect": setting["takes_effect"], "what_it_does": setting["explanation"]}
    if change["from"] == change["to"]:
        return {"changed": False, "note": f"{name} is {change['to']} already"}
    if name in ("surveillance.day", "surveillance.hour"):
        # a moment moved into the last hours counts as reached and not yet run
        at = schedule.waiting(conn, current.with_value(name, checked), now)
        if at is not None and schedule.waiting(conn, current, now) is None:
            change["starts_a_run"] = (
                f"the new moment, {at.isoformat(timespec='minutes')}, has passed less than"
                f" {schedule.GRACE.seconds // 3600} hours ago and has not run: the worker starts"
                " the harvest and the backup within a minute of this change")
    if not confirm:
        return _preview(change)
    settings.set_value(name, checked, path)
    record(conn, member["id"], "settings.set", name, change["from"], change["to"])
    return {"changed": True, **change}


# -- members --------------------------------------------------------------------


def members_request(conn: psycopg.Connection, member: dict, action: str = "list",
                    name: str | None = None, confirm: bool = False) -> dict:
    """list: every member with their activity. add: a new member and their
    key, shown once. revoke: refuse a member's key and login from now on.
    A member who logs in is named by the login provider's identifier."""
    if action == "list":
        rows = members.activity(conn)
        return {"members": [{
            "name": r["name"], "logs_in": r["login"], "admin": r["admin"],
            "status": "revoked" if r["revoked_at"] else "active",
            "added": _iso(r["created_at"]), "revoked": _iso(r["revoked_at"]),
            "last_request": _iso(r["last_request"]), "requests": r["requests"],
            "requests_last_7_days": r["requests_7_days"], "pdf_uploads": r["pdf_uploads"],
            "pdf_uploads_stored": r["pdf_uploads_stored"]} for r in rows],
            "note": "A member the server made at their first login is named by the login"
                    " provider's identifier for the person; a member given a key has the"
                    " name they were given. Admins are made on the host:"
                    " `scriptorium members admin <name>`."}
    name = (name or "").strip()
    if not name:
        raise Invalid(f"{action} needs the member's name")
    if action == "add":
        if members.find(conn, name) is not None:
            raise Invalid(f"there is already a member named {name!r}")
        if not confirm:
            return _preview({"add_member": name, "what_happens":
                             "the member is created and their API key is shown here once; it"
                             " is stored nowhere, so pass it on at once"})
        try:
            key = members.add(conn, name)
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
        record(conn, member["id"], "members.add", name)
        return {"changed": True, "added": name, "api_key": key,
                "note": "This key is shown this once and is not stored. Give it to the member"
                        " privately; they enter it with `scriptorium-client setup`."}
    if action == "revoke":
        row = members.find(conn, name)
        if row is None or row["revoked_at"]:
            raise Invalid(f"no active member {name!r}")
        shown = row["name"]
        if row["id"] == member["id"]:
            raise Invalid("you cannot revoke yourself here; another admin can, or the host")
        if not confirm:
            return _preview({"revoke_member": shown, "what_happens":
                             "their key and their login are refused from now on; what they"
                             " uploaded, their collections and the usage log stay"})
        members.revoke(conn, name)
        record(conn, member["id"], "members.revoke", shown, "active", "revoked")
        return {"changed": True, "revoked": shown}
    raise Invalid("action must be list, add or revoke")


# -- venues ---------------------------------------------------------------------


def venues_request(conn: psycopg.Connection, member: dict, current: settings.Settings,
                   action: str = "list", name: str | None = None, contains: str = "",
                   source_id: str | None = None, issns: list[str] | None = None,
                   publisher: str | None = None, category: str | None = None,
                   needs_filter: bool = False, count: int = 50, offset: int = 0,
                   confirm: bool = False) -> dict:
    """list: the subscribed venues, with the items held from each. add:
    subscribe to a venue by its OpenAlex source ID. remove: stop harvesting
    a venue (its items stay)."""
    if action == "list":
        return venues.listing(conn, contains, count, offset)
    name = (name or "").strip()
    if not name:
        raise Invalid(f"{action} needs the venue's name")
    known = venues.find(conn, name)
    window = int(current["surveillance.window_days"])
    if action == "add":
        source_id = (source_id or "").strip().rsplit("/", 1)[-1]
        if not (source_id.startswith("S") and source_id[1:].isdigit()):
            raise Invalid("add needs source_id, the venue's OpenAlex source ID (S followed by"
                          " digits, e.g. S4210206302); OpenAlex's source search gives it")
        other = conn.execute("SELECT name FROM venues WHERE source_id = %s AND subscribed"
                             " AND lower(name) <> lower(%s)", (source_id, name)).fetchone()
        if other:
            raise Invalid(f"{source_id} is subscribed already, as {other['name']!r}")
        venue = {"name": name, "source_id": source_id, "issns": issns or [],
                 "publisher": publisher, "category": category or "Journals",
                 "needs_filter": needs_filter}
        if not confirm:
            return _preview({"subscribe": venue, "replaces": _venue(known) if known else None,
                             "what_happens":
                             f"from the next harvest on, its papers of the last {window} days"
                             " that pass the harvest rules become items"
                             + (" (only those in the Social Sciences domain)" if needs_filter else "")
                             + "; older papers need a backfill on the host"})
        row = venues.subscribe(conn, name, "openalex", issns=issns, source_id=source_id,
                               publisher=publisher, category=category or "Journals",
                               needs_filter=needs_filter)
        record(conn, member["id"], "venues.add", name, _venue(known) if known else None,
               _venue(row))
        return {"changed": True, "subscribed": _venue(row)}
    if action == "remove":
        if known is None or not known["subscribed"]:
            raise Invalid(f"no subscribed venue named {name!r}; list with contains to find it")
        held = conn.execute("SELECT count(*) AS n FROM items WHERE venue_id = %s",
                            (known["id"],)).fetchone()["n"]
        if not confirm:
            return _preview({"unsubscribe": name, "what_happens":
                             f"it is no longer harvested; its {held} items stay in the"
                             " collection. Adding it again resumes the harvest"})
        venues.unsubscribe(conn, name)
        record(conn, member["id"], "venues.remove", name, "subscribed", "not subscribed")
        return {"changed": True, "unsubscribed": name, "items_kept": held}
    raise Invalid("action must be list, add or remove")


def _venue(row: dict) -> dict:
    return {k: row[k] for k in ("name", "source_id", "issns", "publisher", "category",
                                "needs_filter", "subscribed")}


# -- runs -----------------------------------------------------------------------


def _past_runs(current: settings.Settings, kind: str, count: int) -> list[dict]:
    """The newest finished or started runs of a kind, from their folders:
    harvests from the run folders, backups from the backup folder."""
    out: list[dict] = []
    if kind == "backup":
        for run in reversed(backups.runs(Path(str(current["backup.folder"])))[-count:]):
            manifest = backups.read_manifest(run)
            rows = sum(t["rows"] for t in manifest.get("tables", {}).values())
            files = manifest.get("files", {})
            out.append({"kind": kind, "run": run.name, "status": "ok",
                        "finished": manifest.get("made"),
                        "summary": f"{len(manifest.get('tables', {}))} tables, {rows} rows;"
                                   f" {files.get('pdfs', 0)} PDFs, {files.get('tei', 0)}"
                                   " parsed-text files"})
        return out
    folder = Path(str(current["data.folder"])) / "runs" / kind
    names = sorted((d.name for d in folder.iterdir() if d.is_dir()), reverse=True) \
        if folder.is_dir() else []
    for name in names[:count]:
        manifest = jobs.read_manifest(folder / name)
        out.append({"kind": kind, "run": name, "status": manifest.get("status", "unknown"),
                    "started": manifest.get("started"), "finished": manifest.get("finished"),
                    "summary": manifest.get("summary")})
    return out


def _open_runs(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, kind, status, payload, created_at FROM jobs"
                        " WHERE kind = ANY(%s) AND status IN ('waiting', 'running') ORDER BY id",
                        (list(RUN_KINDS),)).fetchall()
    return [{"kind": r["kind"], "job": r["id"], "status": r["status"],
             "queued": _iso(r["created_at"]),
             **({"dry_run": True} if r["payload"].get("dry_run") else {}),
             "started_by": "the schedule" if r["payload"].get("scheduled") else "an admin"}
            for r in rows]


def runs_request(conn: psycopg.Connection, member: dict, current: settings.Settings,
                 action: str = "list", kind: str | None = None, dry_run: bool = False,
                 count: int = 10, confirm: bool = False,
                 now: datetime.datetime | None = None) -> dict:
    """list: the latest harvests and backups, what is queued or running,
    and the next scheduled moment. start: queue a harvest (surveillance,
    optionally as a dry run) or a backup for the worker to run now."""
    if kind is not None and kind not in RUN_KINDS:
        raise Invalid("kind must be surveillance or backup")
    if action == "list":
        upcoming = schedule.moment(current, now or datetime.datetime.now(), after=True)
        return {"next_scheduled_harvest_and_backup":
                    upcoming.isoformat(timespec="minutes") if upcoming else "switched off",
                "queued_or_running": _open_runs(conn),
                "runs": [r for k in ([kind] if kind else RUN_KINDS)
                         for r in _past_runs(current, k, count)]}
    if action != "start":
        raise Invalid("action must be list or start")
    if kind is None:
        raise Invalid("start needs kind: surveillance or backup")
    if dry_run and kind != "surveillance":
        raise Invalid("only a surveillance run can be a dry run")
    if any(r["kind"] == kind for r in _open_runs(conn)):
        raise Invalid(f"a {kind} run is queued or running already")
    if not confirm:
        what = {"surveillance": "the worker asks the source for the subscribed venues' papers of"
                                f" the last {current['surveillance.window_days']} days"
                                + (" and counts what it would admit; nothing is written"
                                   if dry_run else " and admits the new ones")
                                + f"; cost limit ${current['surveillance.max_cost']}",
                "backup": "the worker writes a backup of the database, the PDFs and the parsed"
                          f" texts, and removes backups older than {current['backup.days_kept']}"
                          " days"}[kind]
        return _preview({"start": kind, **({"dry_run": True} if dry_run else {}),
                         "what_happens": what + ". PDF uploads wait while it runs."})
    payload = {"requested_by": member["name"], **({"dry_run": True} if dry_run else {})}
    job_id = jobs.enqueue(conn, kind, payload)
    record(conn, member["id"], "runs.start", kind, None, {"job": job_id, **payload})
    return {"changed": True, "queued": kind, "job": job_id,
            "note": "The worker starts it when its current job is done; list shows it."}


# -- jobs -----------------------------------------------------------------------


def jobs_request(conn: psycopg.Connection, member: dict, action: str = "list",
                 status: str | None = None, job_id: int | None = None, count: int = 20,
                 confirm: bool = False) -> dict:
    """list: counts by status, failures by reason, and the newest jobs.
    show: one job in full. retry: run a failed job again."""
    if action == "list":
        if status not in (None, "waiting", "running", "done", "failed"):
            raise Invalid("status must be waiting, running, done or failed")
        counts = conn.execute("SELECT kind, status, count(*) AS n FROM jobs"
                              " GROUP BY kind, status ORDER BY kind, status").fetchall()
        reasons = conn.execute(
            "SELECT split_part(error, E'\\n', 1) AS reason, count(*) AS n, max(id) AS newest_job"
            " FROM jobs WHERE status = 'failed' GROUP BY 1 ORDER BY n DESC LIMIT 15").fetchall()
        rows = conn.execute(
            "SELECT j.id, j.kind, j.status, coalesce(m.name, m.subject) AS member, j.created_at, j.finished_at,"
            " j.summary, split_part(j.error, E'\\n', 1) AS error FROM jobs j"
            " LEFT JOIN members m ON m.id = j.member"
            " WHERE %(status)s::text IS NULL OR j.status = %(status)s"
            " ORDER BY j.id DESC LIMIT %(count)s", {"status": status, "count": count}).fetchall()
        return {"counts": counts, "failures_by_reason": reasons,
                "jobs": [{**r, "created_at": _iso(r["created_at"]),
                          "finished_at": _iso(r["finished_at"])} for r in rows],
                "note": "A failure that starts with 'Rejected:' is final: the PDF was not kept."}
    if job_id is None:
        raise Invalid(f"{action} needs job_id")
    row = conn.execute("SELECT j.*, coalesce(m.name, m.subject) AS member_name FROM jobs j"
                       " LEFT JOIN members m ON m.id = j.member WHERE j.id = %s",
                       (job_id,)).fetchone()
    if row is None:
        raise Invalid(f"no job {job_id}")
    if action == "show":
        return {"job": row["id"], "kind": row["kind"], "status": row["status"],
                "member": row["member_name"], "created": _iso(row["created_at"]),
                "started": _iso(row["started_at"]), "finished": _iso(row["finished_at"]),
                "run_folder": row["run_dir"], "payload": row["payload"],
                "summary": row["summary"], "error": row["error"]}
    if action == "retry":
        if row["status"] != "failed":
            raise Invalid(f"job {job_id} is {row['status']}; only a failed job is run again")
        if (row["error"] or "").startswith("Rejected:"):
            raise Invalid(f"job {job_id} was rejected for good ({row['error']}); its file is gone")
        if not confirm:
            return _preview({"retry_job": job_id, "kind": row["kind"],
                             "failed_with": (row["error"] or "").splitlines()[0],
                             "what_happens": "the job waits again and the worker runs it"})
        jobs.retry(conn, job_id)
        record(conn, member["id"], "jobs.retry", str(job_id), "failed", "waiting")
        return {"changed": True, "retried": job_id}
    raise Invalid("action must be list, show or retry")
