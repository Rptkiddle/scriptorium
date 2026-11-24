"""The venues new items are harvested from.

A subscribed venue is harvested by the weekly surveillance. Unsubscribing
stops the harvest and keeps the venue's row and its items. The admin
command and the admin menu both work through these functions.
"""

import psycopg

ADAPTERS = ("crossref", "openalex")


def subscribe(conn: psycopg.Connection, name: str, adapter: str = "openalex", *,
              issns: list[str] | None = None, source_id: str | None = None,
              publisher: str | None = None, category: str | None = None,
              needs_filter: bool = False) -> dict:
    """Subscribe to a venue: add it, or update the one known by that name.
    Returns its row. ValueError for an empty name or an unknown adapter."""
    name = name.strip()
    if not name:
        raise ValueError("a venue needs a name")
    if adapter not in ADAPTERS:
        raise ValueError(f"the adapter must be one of {', '.join(ADAPTERS)}")
    return conn.execute(
        "INSERT INTO venues (name, issns, adapter, source_id, publisher, category,"
        " subscribed, needs_filter) VALUES (%s, %s, %s, %s, %s, %s, true, %s)"
        " ON CONFLICT (lower(name)) DO UPDATE SET issns = EXCLUDED.issns,"
        " adapter = EXCLUDED.adapter, source_id = EXCLUDED.source_id,"
        " publisher = coalesce(EXCLUDED.publisher, venues.publisher),"
        " category = coalesce(EXCLUDED.category, venues.category),"
        " subscribed = true, needs_filter = EXCLUDED.needs_filter RETURNING *",
        (name, issns or [], adapter, source_id, publisher, category, needs_filter)).fetchone()


def unsubscribe(conn: psycopg.Connection, name: str) -> dict | None:
    """Stop harvesting a venue; its items stay. Returns its row, or None
    when no subscribed venue has that name."""
    return conn.execute("UPDATE venues SET subscribed = false WHERE lower(name) = lower(%s)"
                        " AND subscribed RETURNING *", (name.strip(),)).fetchone()


def find(conn: psycopg.Connection, name: str) -> dict | None:
    return conn.execute("SELECT * FROM venues WHERE lower(name) = lower(%s)",
                        (name.strip(),)).fetchone()


def listing(conn: psycopg.Connection, contains: str = "", count: int = 50,
            offset: int = 0) -> dict:
    """The subscribed venues whose name or publisher holds `contains`, by
    name, each with the number of items the collection holds from it:
    {"total", "venues": [...]}."""
    like = f"%{contains.strip()}%"
    where = "subscribed AND (name ILIKE %(like)s OR coalesce(publisher, '') ILIKE %(like)s)"
    total = conn.execute(f"SELECT count(*) AS n FROM venues WHERE {where}",
                         {"like": like}).fetchone()["n"]
    rows = conn.execute(
        f"SELECT v.name, v.publisher, v.category, v.adapter, v.source_id, v.issns,"
        f" v.needs_filter, (SELECT count(*) FROM items WHERE venue_id = v.id) AS items"
        f" FROM venues v WHERE {where} ORDER BY lower(v.name) LIMIT %(count)s OFFSET %(offset)s",
        {"like": like, "count": count, "offset": offset}).fetchall()
    return {"total": total, "venues": rows}
