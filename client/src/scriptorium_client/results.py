"""What enters the conversation, and sets.

The client, not the server, decides how much of a result the agent sees.
fit() returns a result whole when it is small enough; a larger one is
saved whole to a file on this machine, and the agent gets the file's path,
the count and the first page.

A set is such a file. The set tools read it in pages, mark items as
included or excluded (during screening), and export it; its item keys can
be sent back to the server as a filter.
"""

import datetime
import json
import re
from pathlib import Path


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def fit(reply: dict, list_key: str, budget: int, results_dir: str | Path, name: str,
        request: dict | None = None) -> dict:
    """`reply` as the tool should return it. `list_key` names the list in
    the reply that can be long ("items", "passages")."""
    rows = reply.get(list_key) or []
    if _size(reply) <= budget:
        return reply
    folder = Path(results_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "result"
    path = folder / f"{stamp}-{slug}.json"
    n = 1
    while path.exists():
        n += 1
        path = folder / f"{stamp}-{slug}-{n}.json"
    path.write_text(json.dumps({"request": request or {}, **reply, "marks": {}},
                               ensure_ascii=False, indent=1))
    first, used = [], 0
    for row in rows:
        used += _size(row)
        if first and used > budget * 0.8:
            break
        first.append(row)
    rest = {k: v for k, v in reply.items() if k != list_key}
    return {**rest, "saved_to": str(path), "count": len(rows), list_key: first,
            "note": (f"The whole result ({len(rows)} {list_key}) is saved in the file; the first"
                     f" {len(first)} are shown. Read further with set_read, or pass the file as"
                     " `within_set` to a find tool to search only these items.")}


def _load(path: str | Path) -> tuple[Path, dict, str]:
    file = Path(path).expanduser()
    if not file.is_file():
        raise FileNotFoundError(f"no such result file: {file}")
    data = json.loads(file.read_text())
    list_key = "items" if "items" in data else "passages"
    return file, data, list_key


def _row_key(row: dict) -> str:
    return row.get("key") or row.get("item") or ""


def set_read(path: str, page: int = 1, page_size: int = 20) -> dict:
    """One page of a saved result, each row with its mark (included,
    excluded or none)."""
    _, data, list_key = _load(path)
    rows, marks = data[list_key], data.get("marks", {})
    start = (max(page, 1) - 1) * page_size
    shown = [{**row, "mark": {True: "included", False: "excluded"}.get(marks.get(_row_key(row)))}
             for row in rows[start:start + page_size]]
    return {"path": str(path), "count": len(rows), "page": max(page, 1),
            "pages": -(-len(rows) // page_size), list_key: shown,
            "included": sum(1 for v in marks.values() if v),
            "excluded": sum(1 for v in marks.values() if v is False)}


def set_mark(path: str, keys: list[str], included: bool) -> dict:
    """Mark items of a saved result as included or excluded."""
    file, data, list_key = _load(path)
    known = {_row_key(row) for row in data[list_key]}
    unknown = [k for k in keys if k not in known]
    marks = data.setdefault("marks", {})
    marks.update({k: included for k in keys if k in known})
    file.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return {"path": str(file), "marked": len(keys) - len(unknown), "not_in_set": unknown,
            "included": sum(1 for v in marks.values() if v),
            "excluded": sum(1 for v in marks.values() if v is False)}


def set_keys(path: str) -> list[str]:
    """The item keys a set stands for: the included ones once any item is
    marked included, otherwise every item not excluded."""
    _, data, list_key = _load(path)
    marks = data.get("marks", {})
    keys = list(dict.fromkeys(_row_key(row) for row in data[list_key]))
    if any(marks.values()):
        return [k for k in keys if marks.get(k)]
    return [k for k in keys if marks.get(k) is not False]
