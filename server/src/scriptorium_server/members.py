"""Members and their API keys.

A key is made once, shown once and never stored: the database keeps only
its SHA-256 hash. Keys are long random strings, so a plain hash is enough
(there is nothing to guess). Revoking a member keeps the row, and with it
what the member uploaded and the usage log, and refuses the key from then on.

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
    return conn.execute("SELECT id, name, admin FROM members WHERE key_hash = %s"
                        " AND revoked_at IS NULL", (hash_key(key),)).fetchone()


def revoke(conn: psycopg.Connection, name: str) -> bool:
    """Refuse the member's key from now on; False when there is no such
    active member."""
    row = conn.execute("UPDATE members SET revoked_at = now() WHERE name = %s"
                       " AND revoked_at IS NULL RETURNING id", (name.strip(),)).fetchone()
    return row is not None


def listing(conn: psycopg.Connection) -> list[dict]:
    """Every member, oldest first: id, name, created_at, revoked_at."""
    return conn.execute("SELECT id, name, admin, created_at, revoked_at FROM members"
                        " ORDER BY id").fetchall()


def set_admin(conn: psycopg.Connection, name: str, admin: bool = True) -> bool:
    """Make an active member an admin, or an admin a plain member again;
    False when there is no such active member."""
    row = conn.execute("UPDATE members SET admin = %s WHERE name = %s AND revoked_at IS NULL"
                       " RETURNING id", (admin, name.strip())).fetchone()
    return row is not None


def activity(conn: psycopg.Connection) -> list[dict]:
    """Every member with what the usage log and the upload jobs hold about
    them, oldest first: requests in all and in the last seven days, the
    last request, PDFs uploaded and how many of those were stored."""
    return conn.execute(
        "SELECT m.id, m.name, m.admin, m.created_at, m.revoked_at,"
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
