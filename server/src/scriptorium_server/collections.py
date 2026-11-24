"""Collections: a member's own lists of items, kept on the server.

Two kinds. A HISTORY collection is recorded by a workflow run (a search, a
discovery, an interrogation): it keeps what was asked and the items that
came back, each with its rank. An ACTIVE collection is made and named by
the member, who adds and removes items. "Keeping" a history collection
turns it into an active one under a name.

A collection belongs to one member and no other member sees it; every
function here takes the member and looks only at theirs. A collection is
named in requests by its number, or (an active one) by its name.

An item's labels are the names of the collections that hold it:
for_item() gives, for one item, the member's collections and histories it
is in, with the names and what was asked.
"""

import psycopg
from psycopg.types.json import Jsonb

KINDS = ("history", "active")
NAME_LENGTH = 200


class NotFound(LookupError):
    """No collection of this member answers to the reference."""


class Invalid(ValueError):
    """A request that cannot be carried out; the message says why."""


def _name(name: str | None) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise Invalid("a collection needs a name")
    if name.isdigit():
        raise Invalid("a name of digits only would be taken for a collection's number")
    if len(name) > NAME_LENGTH:
        raise Invalid(f"a collection's name has at most {NAME_LENGTH} characters")
    return name


def find(conn: psycopg.Connection, member: int, ref: str | int) -> dict:
    """The member's collection with this number, or their active collection
    of this name. NotFound otherwise."""
    ref = str(ref).strip()
    if ref.isdigit():
        row = conn.execute("SELECT * FROM collections WHERE id = %s AND member = %s",
                           (int(ref), member)).fetchone()
    else:
        row = conn.execute("SELECT * FROM collections WHERE member = %s AND kind = 'active'"
                           " AND lower(name) = lower(%s)", (member, " ".join(ref.split()))).fetchone()
    if row is None:
        raise NotFound(f"you have no collection {ref!r}; `collections` lists them")
    return row


def shown(row: dict, items: int | None = None) -> dict:
    out = {"collection": row["id"], "kind": row["kind"], "name": row["name"],
           "created": row["created_at"].isoformat(timespec="seconds"),
           "updated": row["updated_at"].isoformat(timespec="seconds")}
    if row["kind"] == "history":
        out.update(workflow=row["workflow"], request=row["request"])
    if items is not None:
        out["item_count"] = items
    return out


def size(conn: psycopg.Connection, collection: int) -> int:
    return conn.execute("SELECT count(*) AS n FROM collection_items WHERE collection = %s",
                        (collection,)).fetchone()["n"]


def listing(conn: psycopg.Connection, member: int, kind: str | None = None, contains: str = "",
            count: int = 50, offset: int = 0) -> dict:
    """The member's collections, newest first, each with its number of
    items: {"total", "collections"}. `contains` narrows by a part of the
    name or of what was asked."""
    if kind not in (None, *KINDS):
        raise Invalid("kind must be history or active")
    where = ("c.member = %(member)s AND (%(kind)s::text IS NULL OR c.kind = %(kind)s)"
             " AND (c.name ILIKE %(like)s OR coalesce(c.request::text, '') ILIKE %(like)s)")
    values = {"member": member, "kind": kind, "like": f"%{contains.strip()}%",
              "count": count, "offset": offset}
    total = conn.execute(f"SELECT count(*) AS n FROM collections c WHERE {where}",
                         values).fetchone()["n"]
    rows = conn.execute(
        f"SELECT c.*, (SELECT count(*) FROM collection_items WHERE collection = c.id) AS n"
        f" FROM collections c WHERE {where} ORDER BY c.created_at DESC, c.id DESC"
        " LIMIT %(count)s OFFSET %(offset)s", values).fetchall()
    return {"total": total, "collections": [shown(r, r["n"]) for r in rows]}


def keys(conn: psycopg.Connection, member: int, refs: list[str | int]) -> list[str]:
    """The keys of the items in any of the member's named collections (for
    the `collections` filter of the find requests)."""
    ids = [find(conn, member, ref)["id"] for ref in refs]
    rows = conn.execute("SELECT DISTINCT item FROM collection_items WHERE collection = ANY(%s)",
                        (ids,)).fetchall()
    return [r["item"] for r in rows]


def ordered_keys(conn: psycopg.Connection, collection: int, count: int, offset: int) -> list[dict]:
    """A page of a collection's items in its own order: by rank where the
    items have one (a history), else by when they were added."""
    return conn.execute(
        "SELECT item, rank, added_at FROM collection_items WHERE collection = %s"
        " ORDER BY rank NULLS LAST, added_at, item LIMIT %s OFFSET %s",
        (collection, count, offset)).fetchall()


def _held(conn: psycopg.Connection, wanted: list[str]) -> tuple[list[str], list[str]]:
    """(the item keys the inputs name, in their order and once each; the
    inputs that name no held item). An input is an item key or a citation key."""
    wanted = list(dict.fromkeys(k.strip() for k in wanted if k and k.strip()))
    rows = conn.execute("SELECT key, citation_key FROM items WHERE key = ANY(%(k)s)"
                        " OR citation_key = ANY(%(k)s)", {"k": wanted}).fetchall()
    by_any = {r["key"]: r["key"] for r in rows} | {
        r["citation_key"]: r["key"] for r in rows if r["citation_key"]}
    found = list(dict.fromkeys(by_any[k] for k in wanted if k in by_any))
    return found, [k for k in wanted if k not in by_any]


def _put(conn: psycopg.Connection, collection: int, item_keys: list[str], ranked: bool) -> int:
    """Add items to a collection, skipping those it holds; returns how many
    were new. With `ranked` each new item takes the next rank."""
    if not item_keys:
        return 0
    start = conn.execute("SELECT coalesce(max(rank), 0) AS top FROM collection_items"
                         " WHERE collection = %s", (collection,)).fetchone()["top"] if ranked else 0
    rows = conn.execute(
        "INSERT INTO collection_items (collection, item, rank)"
        " SELECT %(c)s, k.item,"
        "   CASE WHEN %(ranked)s THEN %(start)s + row_number() OVER (ORDER BY k.n) END"
        " FROM unnest(%(keys)s::text[]) WITH ORDINALITY AS k(item, n)"
        " WHERE NOT EXISTS (SELECT 1 FROM collection_items h"
        "                   WHERE h.collection = %(c)s AND h.item = k.item)"
        " ON CONFLICT DO NOTHING RETURNING item",
        {"c": collection, "keys": item_keys, "ranked": ranked, "start": start}).fetchall()
    conn.execute("UPDATE collections SET updated_at = now() WHERE id = %s", (collection,))
    return len(rows)


def add(conn: psycopg.Connection, member: int, collection: str | int, item_keys: list[str],
        from_collection: str | int | None = None) -> dict:
    """Add items to an active collection, named by its number or its name;
    a name the member has no collection of makes a new one. `from_collection`
    adds every item of another of the member's collections as well."""
    try:
        row = find(conn, member, collection)
        created = False
    except NotFound:
        if str(collection).strip().isdigit():
            raise
        row = conn.execute("INSERT INTO collections (member, kind, name) VALUES (%s, 'active', %s)"
                           " RETURNING *", (member, _name(str(collection)))).fetchone()
        created = True
    if row["kind"] != "active":
        raise Invalid(f"collection {row['id']} is a history; keep it under a name first"
                      " (edit with keep), or add its items to an active collection")
    found, missing = _held(conn, item_keys)
    if from_collection is not None:
        source = find(conn, member, from_collection)
        found += [r["item"] for r in conn.execute(
            "SELECT item FROM collection_items WHERE collection = %s"
            " ORDER BY rank NULLS LAST, added_at, item", (source["id"],))]
        found = list(dict.fromkeys(found))
    added = _put(conn, row["id"], found, ranked=False)
    return {**shown(row, size(conn, row["id"])), "new_collection": created, "added": added,
            "already_in_it": len(found) - added, "not_held": missing}


def record(conn: psycopg.Connection, member: int, workflow: str, name: str,
           request: dict | None, item_keys: list[str],
           collection: str | int | None = None) -> dict:
    """Record a workflow run as a history collection: what was asked and
    the items that came back, in their order. A run with more items than
    one request may carry sends the rest with `collection` set to the
    number the first call returned; their ranks continue."""
    if collection is not None:
        row = find(conn, member, collection)
        if row["kind"] != "history":
            raise Invalid(f"collection {row['id']} is not a history")
    else:
        workflow = " ".join((workflow or "").split()).lower()
        if not workflow:
            raise Invalid("a history needs the workflow that made it, e.g. search")
        row = conn.execute(
            "INSERT INTO collections (member, kind, name, workflow, request)"
            " VALUES (%s, 'history', %s, %s, %s) RETURNING *",
            (member, _name(name), workflow, Jsonb(request or {}))).fetchone()
    found, missing = _held(conn, item_keys)
    added = _put(conn, row["id"], found, ranked=True)
    return {**shown(row, size(conn, row["id"])), "added": added, "not_held": missing}


def remove(conn: psycopg.Connection, member: int, collection: str | int,
           item_keys: list[str]) -> dict:
    """Take items out of one of the member's collections."""
    row = find(conn, member, collection)
    found, missing = _held(conn, item_keys)
    gone = conn.execute("DELETE FROM collection_items WHERE collection = %s AND item = ANY(%s)"
                        " RETURNING item", (row["id"], found)).fetchall()
    conn.execute("UPDATE collections SET updated_at = now() WHERE id = %s", (row["id"],))
    return {**shown(row, size(conn, row["id"])), "removed": len(gone),
            "not_in_it": len(found) - len(gone) + len(missing)}


def edit(conn: psycopg.Connection, member: int, collection: str | int, name: str | None = None,
         keep: bool = False, delete: bool = False) -> dict:
    """Rename a collection, keep a history as an active collection (under
    `name`, else its own), or delete a collection of either kind. The items
    themselves are never touched."""
    row = find(conn, member, collection)
    if delete:
        if name or keep:
            raise Invalid("delete goes alone, without name or keep")
        held = size(conn, row["id"])
        conn.execute("DELETE FROM collections WHERE id = %s", (row["id"],))
        return {"deleted": row["id"], "kind": row["kind"], "name": row["name"],
                "items_it_held": held}
    if not (name or keep):
        raise Invalid("edit needs a name, keep or delete")
    new_name = _name(name) if name else row["name"]
    kind = "active" if keep or row["kind"] == "active" else "history"
    try:
        with conn.transaction():
            row = conn.execute("UPDATE collections SET name = %s, kind = %s, updated_at = now()"
                               " WHERE id = %s RETURNING *", (new_name, kind, row["id"])).fetchone()
    except psycopg.errors.UniqueViolation:
        raise Invalid(f"you already have an active collection named {new_name!r}") from None
    return shown(row, size(conn, row["id"]))


def for_item(conn: psycopg.Connection, member: int, item: str) -> list[dict]:
    """The member's collections that hold an item, newest first: the
    labels it was filed under and the questions that found it."""
    rows = conn.execute(
        "SELECT c.*, ci.rank FROM collection_items ci JOIN collections c ON c.id = ci.collection"
        " WHERE ci.item = %s AND c.member = %s ORDER BY c.created_at DESC, c.id DESC",
        (item, member)).fetchall()
    return [{**shown(r), **({"rank": r["rank"]} if r["rank"] is not None else {})} for r in rows]
