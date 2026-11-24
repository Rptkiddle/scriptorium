"""The web page: what Scriptorium is, open to anyone past the front door,
and, once a member signs in, a dashboard to see and search the collection.

The page itself (web/index.html) holds no data. Signing in goes through
the login provider (login.issuer, oidc.py) the client logs in at too:
every person it lets in is a member, found or made by the provider's
subject (members.for_login), so the page and the client lead to one
member. The person's name is not stored with the member; the session
keeps it, for the page's header, until it ends. The browser then holds a
session cookie, HttpOnly, so the page's script never sees it.

The dashboard calls the same /v1 requests as the client, which, like
every request, go through auth.handle(). There a session counts only on a
request that also carries the header WEB_HEADER: a page on another site
cannot add that header without the server's consent (CORS, which this
server never gives), so it cannot make a signed-in browser send requests
(no cross-site request forgery).
"""

import hashlib
import secrets
from datetime import timedelta
from pathlib import Path

import psycopg

PAGE = Path(__file__).parent / "web" / "index.html"
SESSION_COOKIE = "scriptorium_session"
LOGIN_COOKIE = "scriptorium_login"   # binds a sign-in under way to the browser that started it
WEB_HEADER = "x-scriptorium-web"
LOGIN_MINUTES = 10                   # how long a sign-in may take at the provider


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def contents(conn: psycopg.Connection) -> dict:
    """The number of items and of venues with at least one item."""
    row = conn.execute("SELECT count(*) AS items, count(DISTINCT venue_id) AS venues"
                       " FROM items").fetchone()
    return dict(row)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- a sign-in under way ---------------------------------------------------------


def start_login(conn: psycopg.Connection, verifier: str) -> tuple[str, str]:
    """Record a new sign-in; returns (state, nonce). Sign-ins left
    unfinished are removed."""
    conn.execute("DELETE FROM web_logins WHERE created_at < now() - %s",
                 (timedelta(minutes=LOGIN_MINUTES),))
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    conn.execute("INSERT INTO web_logins (state, nonce, verifier) VALUES (%s, %s, %s)",
                 (state, nonce, verifier))
    return state, nonce


def take_login(conn: psycopg.Connection, state: str) -> dict | None:
    """The sign-in the state names, used up; None when unknown or too old."""
    return conn.execute(
        "DELETE FROM web_logins WHERE state = %s AND created_at >= now() - %s"
        " RETURNING nonce, verifier", (state, timedelta(minutes=LOGIN_MINUTES))).fetchone()


# --- sessions --------------------------------------------------------------------


def shown_name(claims: dict) -> str | None:
    """What the page's header calls the person: the ID token's name, else
    username, else email address."""
    return next((str(claims[k]).strip() for k in ("name", "preferred_username", "email")
                 if str(claims.get(k) or "").strip()), None)


def start_session(conn: psycopg.Connection, member: int, hours: int,
                  name: str | None = None) -> str:
    """A new session for the member; returns the cookie's value, which is
    not kept (the database holds its hash). Expired sessions are removed."""
    conn.execute("DELETE FROM web_sessions WHERE expires_at < now()")
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO web_sessions (token_hash, member, shown_name, expires_at)"
                 " VALUES (%s, %s, %s, now() + %s)",
                 (_hash(token), member, name, timedelta(hours=hours)))
    return token


def session_member(conn: psycopg.Connection, token: str) -> dict | None:
    """The member of a live session; None for an unknown or expired
    session, or a revoked member."""
    if not token:
        return None
    return conn.execute(
        "SELECT m.id, coalesce(m.name, m.subject) AS name, m.admin, s.shown_name"
        " FROM web_sessions s JOIN members m ON m.id = s.member"
        " WHERE s.token_hash = %s AND s.expires_at > now() AND m.revoked_at IS NULL",
        (_hash(token),)).fetchone()


def end_session(conn: psycopg.Connection, token: str) -> None:
    if token:
        conn.execute("DELETE FROM web_sessions WHERE token_hash = %s", (_hash(token),))
