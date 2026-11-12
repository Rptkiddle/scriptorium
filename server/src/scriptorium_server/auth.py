"""Authentication, the two limits and the usage log, in one place: every
request of a member passes through handle().

handle() finds the member for the API key (401 otherwise), refuses a
client older than the server supports (426), applies the rate per member
(429), runs the request, and writes one usage_log row whatever the
outcome: member, request name, all inputs, the full reply or the error,
status, duration, client version, agent name. No network address is read
or stored.
"""

import time
from collections.abc import Callable
from contextlib import AbstractContextManager

import psycopg
from fastapi import Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from scriptorium_server import members
from scriptorium_server.retrieval import BadRequest

CLIENT_HEADER = "x-scriptorium-client"   # the client's version
AGENT_HEADER = "x-scriptorium-agent"     # the name of the agent using the client
MIN_CLIENT = (0, 2, 0)                   # the oldest client version this server serves (0.2.0: the notice)

Connect = Callable[[], AbstractContextManager[psycopg.Connection]]


class Refused(Exception):
    """A request the server will not serve: an HTTP status and a plain message."""

    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def _version(text: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in text.strip().split(".")[:3])
    except ValueError:
        return None


def check_client(version_header: str | None) -> None:
    """Refuse a client older than MIN_CLIENT. A request that names no
    client version (not sent by the client) is let through."""
    if not version_header:
        return
    version = _version(version_header)
    if version is None or version < MIN_CLIENT:
        wanted = ".".join(map(str, MIN_CLIENT))
        raise Refused(426, f"update the client: this server needs Scriptorium client {wanted}"
                           f" or newer, and this one is {version_header}")


def check_limit(count: int | None, per_request: int, what: str = "results") -> None:
    """Refuse a request for more than the per-request maximum."""
    if count is not None and count > per_request:
        raise Refused(400, f"this request asks for {count} {what}; the most one request may ask"
                           f" for is {per_request} (the server's limit per request)")


def _key(request: Request) -> str:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def handle(connect: Connect, request: Request, name: str, inputs: dict,
           run: Callable[[psycopg.Connection, dict], dict], per_minute: int):
    """Serve one request of a member and log it. `run(conn, member)` does
    the work and returns the reply as a dict (or a ready response, whose
    `logged` attribute is what the log keeps)."""
    started = time.monotonic()
    member_id, status, logged = None, 200, None
    client, agent = request.headers.get(CLIENT_HEADER), request.headers.get(AGENT_HEADER)
    with connect() as conn:
        try:
            member = members.authenticate(conn, _key(request))
            if member is None:
                raise Refused(401, "unknown or revoked API key")
            member_id = member["id"]
            check_client(client)
            recent = conn.execute(
                "SELECT count(*) AS n FROM usage_log WHERE member = %s"
                " AND at > now() - interval '1 minute'", (member_id,)).fetchone()["n"]
            if recent >= per_minute:
                raise Refused(429, f"rate limit: a member may send at most {per_minute} requests"
                                   " a minute (the server's rate per member); wait and try again")
            reply = run(conn, member)
            conn.commit()
            logged = getattr(reply, "logged", reply)
            return reply
        except (Refused, BadRequest) as exc:
            conn.rollback()
            status = exc.status if isinstance(exc, Refused) else 400
            logged = {"error": str(exc)}
            return JSONResponse(logged, status_code=status)
        except Exception as exc:
            conn.rollback()
            status, logged = 500, {"error": f"{type(exc).__name__}: {exc}"}
            raise
        finally:
            conn.execute(
                "INSERT INTO usage_log (member, request, inputs, reply, status, duration_ms,"
                " client_version, agent) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (member_id, name, Jsonb(inputs), Jsonb(logged), status,
                 int((time.monotonic() - started) * 1000), client, agent))
            conn.commit()
