"""The weekly schedule: the worker starts the harvest and the backup itself.

The settings surveillance.day and surveillance.hour name one moment a week.
When it has come, the worker queues a surveillance job and, behind it, a
backup job, once: each job carries the moment it was queued for, and a
moment that already has its job is not queued again. A worker that was
not running at the moment queues the jobs when it starts, if no more than
GRACE has passed; a moment missed by more waits for the next week.
"""

import datetime

import psycopg

from scriptorium_server import jobs
from scriptorium_server.settings import DAYS, Settings

GRACE = datetime.timedelta(hours=12)
KINDS = ("surveillance", "backup")  # queued in this order, so the backup holds the harvest


def moment(current: Settings, now: datetime.datetime, *, after: bool = False
           ) -> datetime.datetime | None:
    """The scheduled moment last reached at `now`, or with `after` the
    next one to come; None when the schedule is switched off."""
    day = str(current["surveillance.day"])
    if day not in DAYS:
        return None
    at = now.replace(hour=int(current["surveillance.hour"]), minute=0, second=0, microsecond=0)
    at -= datetime.timedelta(days=(now.weekday() - DAYS.index(day)) % 7)
    if at > now:
        at -= datetime.timedelta(days=7)
    return at + datetime.timedelta(days=7) if after else at


def waiting(conn: psycopg.Connection, current: Settings,
            now: datetime.datetime | None = None) -> datetime.datetime | None:
    """The scheduled moment whose runs are due and not queued yet, if any."""
    now = now or datetime.datetime.now()
    at = moment(current, now)
    if at is None or now - at > GRACE:
        return None
    queued = conn.execute(
        "SELECT 1 FROM jobs WHERE kind = ANY(%s) AND payload->>'scheduled' = %s LIMIT 1",
        (list(KINDS), at.isoformat(timespec="minutes"))).fetchone()
    return None if queued else at


def due(conn: psycopg.Connection, current: Settings,
        now: datetime.datetime | None = None) -> list[int]:
    """Queue the week's harvest and backup when their moment has come and
    they are not queued yet; returns the new jobs' ids."""
    at = waiting(conn, current, now)
    ids = [] if at is None else [
        jobs.enqueue(conn, kind, {"scheduled": at.isoformat(timespec="minutes")})
        for kind in KINDS]
    conn.commit()
    return ids
