"""Members and their API keys.

A key is made once, shown once and never stored: the database keeps only
its SHA-256 hash. Keys are long random strings, so a plain hash is enough
(there is nothing to guess). Revoking a member keeps the row, and with it
what the member donated and the usage log, and refuses the key from then on.
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
    return conn.execute("SELECT id, name FROM members WHERE key_hash = %s"
                        " AND revoked_at IS NULL", (hash_key(key),)).fetchone()


def revoke(conn: psycopg.Connection, name: str) -> bool:
    """Refuse the member's key from now on; False when there is no such
    active member."""
    row = conn.execute("UPDATE members SET revoked_at = now() WHERE name = %s"
                       " AND revoked_at IS NULL RETURNING id", (name.strip(),)).fetchone()
    return row is not None


def listing(conn: psycopg.Connection) -> list[dict]:
    """Every member, oldest first: id, name, created_at, revoked_at."""
    return conn.execute("SELECT id, name, created_at, revoked_at FROM members"
                        " ORDER BY id").fetchall()
