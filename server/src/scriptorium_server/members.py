"""Members and their API keys.

A key is made once, shown once and never stored: the database keeps only
its SHA-256 hash. Keys are long random strings, so a plain hash is enough
(there is nothing to guess). Revoking a member keeps the row, and with it
what the member uploaded and the usage log, and refuses the key from then on.

A member who logs in through the login provider (login.py) is known by
the provider's address and its identifier for the person. The server makes
that row at the first login, with no name and no key; such a member is
named by the provider's identifier. A member with a key who logs in while
sending the key keeps their row: the login is attached to it. Signing in
to the web page (web.py) goes through the same provider and finds or makes
the same row, so the client and the page lead to one member.

An admin is a member who may use the admin menu. The mark is set and
removed on the host only, so a stolen admin key cannot make more admins.
"""

import hashlib
import secrets

import psycopg

KEY_PREFIX = "scr_"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


def add(conn: psycopg.Connection, name: str) -> str:
    """Create a member and return the API key, which is not kept anywhere.
    ValueError when the name is empty or taken."""
    name = name.strip()
    if not name:
        raise ValueError("a member needs a name")
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    try:
        with conn.transaction():
            conn.execute("INSERT INTO members (name, key_hash) VALUES (%s, %s)",
                         (name, hash_key(key)))
    except psycopg.errors.UniqueViolation:
        raise ValueError(f"there is already a member named {name!r}") from None
    return key


def authenticate(conn: psycopg.Connection, key: str) -> dict | None:
    """The member row a key belongs to; None for an unknown or revoked key."""
    if not key:
        return None
    return conn.execute("SELECT id, name, admin, subject FROM members WHERE key_hash = %s"
                        " AND revoked_at IS NULL", (hash_key(key),)).fetchone()


# -- members who log in ---------------------------------------------------------


def by_login(conn: psycopg.Connection, issuer: str, subject: str) -> dict | None:
    """The member row of a login, revoked or not; None when the person has
    not logged in before."""
    return conn.execute("SELECT id, name, admin, subject, revoked_at FROM members"
                        " WHERE issuer = %s AND subject = %s", (issuer, subject)).fetchone()


def create_login(conn: psycopg.Connection, issuer: str, subject: str) -> dict:
    """The new member row of a first login: a number and the login, nothing else."""
    conn.execute("INSERT INTO members (issuer, subject) VALUES (%s, %s)"
                 " ON CONFLICT DO NOTHING", (issuer, subject))
    return by_login(conn, issuer, subject)


def attach_login(conn: psycopg.Connection, member: int, issuer: str, subject: str) -> dict:
    """Attach a login to a member who has a key, so both lead to one row."""
    conn.execute("UPDATE members SET issuer = %s, subject = %s WHERE id = %s",
                 (issuer, subject, member))
    return by_login(conn, issuer, subject)


def for_login(conn: psycopg.Connection, issuer: str, subject: str) -> dict:
    """The member row of a login, made at the first login; revoked or not."""
    return by_login(conn, issuer, subject) or create_login(conn, issuer, subject)


# -- naming a member ------------------------------------------------------------

SHOWN = "coalesce(name, subject)"  # a member's name: the one given, else the login's identifier


def find(conn: psycopg.Connection, who: str) -> dict | None:
    """The member a name refers to: the name given with a key, or the
    identifier of a member who logs in."""
    return conn.execute(f"SELECT id, {SHOWN} AS name, admin, revoked_at FROM members"
                        " WHERE name = %(who)s OR (name IS NULL AND subject = %(who)s)",
                        {"who": who.strip()}).fetchone()


def revoke(conn: psycopg.Connection, who: str) -> bool:
    """Refuse the member's key and login from now on; False when there is
    no such active member."""
    found = find(conn, who)
    if found is None or found["revoked_at"]:
        return False
    conn.execute("UPDATE members SET revoked_at = now() WHERE id = %s", (found["id"],))
    return True


def listing(conn: psycopg.Connection) -> list[dict]:
    """Every member, oldest first: id, name (the login's identifier for a
    member without one), whether they log in, admin, created_at, revoked_at."""
    return conn.execute(f"SELECT id, {SHOWN} AS name, subject IS NOT NULL AS login, admin,"
                        " created_at, revoked_at FROM members ORDER BY id").fetchall()


def set_admin(conn: psycopg.Connection, who: str, admin: bool = True) -> bool:
    """Make an active member an admin, or an admin a plain member again;
    False when there is no such active member."""
    found = find(conn, who)
    if found is None or found["revoked_at"]:
        return False
    conn.execute("UPDATE members SET admin = %s WHERE id = %s", (admin, found["id"]))
    return True


def activity(conn: psycopg.Connection) -> list[dict]:
    """Every member with what the usage log and the upload jobs hold about
    them, oldest first: requests in all and in the last seven days, the
    last request, PDFs uploaded and how many of those were stored."""
    return conn.execute(
        "SELECT m.id, coalesce(m.name, m.subject) AS name,"
        " m.subject IS NOT NULL AS login, m.admin, m.created_at, m.revoked_at,"
        " coalesce(u.requests, 0) AS requests, coalesce(u.requests_7_days, 0) AS requests_7_days,"
        " u.last_request, coalesce(j.uploads, 0) AS pdf_uploads,"
        " coalesce(j.stored, 0) AS pdf_uploads_stored"
        " FROM members m"
        " LEFT JOIN (SELECT member, count(*) AS requests, max(at) AS last_request,"
        "   count(*) FILTER (WHERE at > now() - interval '7 days') AS requests_7_days"
        "   FROM usage_log WHERE member IS NOT NULL GROUP BY member) u ON u.member = m.id"
        " LEFT JOIN (SELECT member, count(*) AS uploads,"
        "   count(*) FILTER (WHERE status = 'done' AND summary LIKE 'stored:%') AS stored"
        "   FROM jobs WHERE kind = 'fulltext' AND member IS NOT NULL GROUP BY member) j"
        "   ON j.member = m.id ORDER BY m.id").fetchall()
