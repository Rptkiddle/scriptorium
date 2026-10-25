"""Zotero writer: local writes via the Scriptorium Bridge plugin.

Zotero's local API is read-only, so writes go through the bridge plugin
(zotero-bridge/ in this repo), which registers write endpoints on Zotero's
localhost server. Every change lands in the local library instantly;
Zotero's own sync uploads it in the background. A running Zotero with the
plugin installed is scriptorium's baseline requirement for writes — when
either is missing, operations fail with a clear error and the caller
reports it rather than falling back.

Attachment files stay local as linked files under the configured base
directory (zotero.linked_attachments_dir); Zotero holds only the path.

Every write, successful or not, is appended to <data>/writes.jsonl: time,
source (the run or caller that asked for it), operation, request, the
bridge's response or the error, and for updates and deletes the item's
state just before the write. The log is never rewritten, so any change to
the library can be audited and undone from the data directory.
"""

import datetime
import hashlib
import html
import json
import re
import shutil
from pathlib import Path

from scriptorium.citekeys import generate_citekey, unique_key
from scriptorium.config import Config
from scriptorium.settings import load_settings
from scriptorium.zotero import ZoteroReader
from scriptorium.zotero_bridge import BridgeError, BridgeTimeout, BridgeUnavailable, ZoteroBridge

# Item types that never carry a citation key.
_NO_CITEKEY_TYPES = {"attachment", "note", "annotation"}

_ENTITY = re.compile(r"&(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);")


def _unescape_entities(value):
    """Recursively HTML-unescape strings in item data. Source metadata
    (Crossref especially) sometimes carries escaped entities ("New Media
    &amp; Society"); Zotero stores field values literally, so they must be
    unescaped before writing."""
    if isinstance(value, str):
        while _ENTITY.search(value):
            value = html.unescape(value)
        return value
    if isinstance(value, list):
        return [_unescape_entities(v) for v in value]
    if isinstance(value, dict):
        return {k: _unescape_entities(v) for k, v in value.items()}
    return value


def _clean_list(values, what: str) -> list[str]:
    """A list of names or keys: entities unescaped, whitespace stripped,
    duplicates dropped, order kept. A bare string or an empty entry is refused."""
    if isinstance(values, str):
        raise ZoteroWriteError(f"give the {what}s as a list, e.g. [{values!r}]")
    out = []
    for value in values or ():
        if not isinstance(value, str) or not value.strip():
            raise ZoteroWriteError(f"each {what} must be a non-empty string")
        value = _unescape_entities(value).strip()
        if value not in out:
            out.append(value)
    return out


def _list_edit(item_keys, add, remove, what: str) -> tuple[list[str], list[str], list[str]]:
    """Checked arguments for an add/remove edit of a list field."""
    keys = _clean_list(item_keys, "item key")
    add, remove = _clean_list(add, what), _clean_list(remove, what)
    if not keys:
        raise ZoteroWriteError("give at least one item key")
    if not add and not remove:
        raise ZoteroWriteError(f"give at least one {what} to add or remove")
    both = sorted(set(add) & set(remove))
    if both:
        raise ZoteroWriteError(f"{what}s both added and removed: {', '.join(both)}")
    return keys, add, remove


# Tags a Zotero note uses; other angle-bracket text ("<line>") is plain text.
_HTML_TAG = re.compile(
    r"</?(p|br|div|span|b|i|u|s|em|strong|a|ul|ol|li|h[1-6]|blockquote|pre|code|sub|sup"
    r"|table|tr|td|th)\b[^>]*>", re.IGNORECASE)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _note_html(note: str) -> str:
    """A note's HTML: kept as given when it already holds HTML tags;
    otherwise each blank-line-separated paragraph becomes <p>, with line
    breaks inside it kept."""
    if not isinstance(note, str) or not note.strip():
        raise ZoteroWriteError("a note cannot be empty")
    if _HTML_TAG.search(note):
        return note
    paragraphs = re.split(r"\n\s*\n", note.strip())
    return "".join(
        "<p>" + "<br>".join(html.escape(line) for line in p.splitlines()) + "</p>"
        for p in paragraphs
    )


_UNAVAILABLE = (
    "Zotero writes need the Zotero app running with the Scriptorium Bridge"
    " plugin installed (zotero-bridge/ in the scriptorium repo)."
)


class ZoteroWriteError(RuntimeError):
    """Raised when a write cannot be performed (Zotero/bridge down, bad data)."""


WRITE_LOG = "writes.jsonl"
# Venue folders: [✓] subscribed, [✗] decided against. Both record a decision
# and are kept even when empty.
VENUE_PREFIXES = ("[✓]", "[✗]")


class ZoteroWriter:
    """Write access to the Zotero library through the bridge plugin.
    `source` names the caller in the write log (a run, or "daemon");
    each method can override it per call."""

    def __init__(self, config: Config, source: str = "daemon",
                 reader: ZoteroReader | None = None, on_write=None):
        self.config = config
        self.source = source
        # Called after every write that reached Zotero (the daemon uses it to
        # bring its index up to date at once).
        self._on_write = on_write
        self._bridge = ZoteroBridge()
        # Reads (citation keys, before-states) go through a ZoteroReader and
        # its lock; the daemon passes its own so one locked client serves
        # all threads.
        self._reader = reader or ZoteroReader(config)

    def _settings(self) -> dict:
        return load_settings(self.config)["zotero"]

    def _taken_citekeys(self) -> set[str]:
        """Citation keys already in use, read from the local API. Empty when
        Zotero is closed — generation then proceeds without collision checks
        (collisions are rare and suffixes are cosmetic)."""
        try:
            return self._reader.citation_keys()
        except Exception:
            return set()

    def _before(self, item_key: str) -> dict:
        """The item's current data, for the write log; a failed read is
        recorded rather than blocking the write."""
        try:
            data = self._reader.item_raw(item_key)
        except Exception as exc:  # noqa: BLE001 — logged; the write decides
            return {"unavailable": f"{type(exc).__name__}: {exc}"[:200]}
        return data if data is not None else {"unavailable": "item not found"}

    def _log(self, entry: dict) -> None:
        with (self.config.index_dir / WRITE_LOG).open("a") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _call(self, operation: str, fn, *args, request=None, before=None,
              source: str | None = None, **kwargs) -> dict:
        entry = {
            "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": source or self.source, "operation": operation,
            "request": request, "before": before,
        }
        try:
            result = fn(*args, **kwargs)
        except BridgeUnavailable as exc:
            self._log({**entry, "error": f"BridgeUnavailable: {exc}"})
            raise ZoteroWriteError(f"{_UNAVAILABLE} ({exc})") from exc
        except BridgeError as exc:
            self._log({**entry, "error": f"{type(exc).__name__}: {exc}"})
            raise ZoteroWriteError(f"{operation} failed: {exc}") from exc
        self._log({**entry, "response": result})
        if self._on_write is not None:
            self._on_write()
        return result

    def create_items(self, items: list[dict], collection_key: str = "",
                     source: str | None = None,
                     taken: set[str] | None = None) -> dict:
        """Create items from complete Zotero JSON data dicts. Optionally place
        each in a collection. Items without a citationKey get one generated
        (BBT-default format). Returns {"created": [keys], "failed": {...},
        "citation_keys": {item_key: citekey}} so callers can use the keys
        (e.g. to name attached PDFs). `taken` is the set of citation keys
        in use, when the caller has already read it; it gains the new keys."""
        items = [_unescape_entities(dict(it)) for it in items]
        if collection_key:
            items = [
                {**it, "collections": sorted({*it.get("collections", []), collection_key})}
                for it in items
            ]
        keyed = [it for it in items if it.get("itemType") not in _NO_CITEKEY_TYPES]
        if keyed:
            if taken is None:
                taken = self._taken_citekeys()
            for it in keyed:
                # A supplied key is kept unless it collides; then it gets a suffix.
                it["citationKey"] = (unique_key(it["citationKey"], taken)
                                     if it.get("citationKey") else generate_citekey(it, taken))
                taken.add(it["citationKey"])
        return self._call("create", self._bridge.create_items, items,
                          request={"items": items}, source=source)

    def update_item(self, item_key: str, patch: dict, source: str | None = None) -> dict:
        """Apply a field patch to an existing item."""
        patch = _unescape_entities(patch)
        return self._call(
            f"update of {item_key}", self._bridge.update_item, item_key, patch,
            request={"key": item_key, "patch": patch},
            before=self._before(item_key), source=source,
        )

    def _list_before(self, keys: list[str], field: str) -> dict:
        """Each item's list field before a list edit, for the write log."""
        before = {}
        for key in keys:
            data = self._before(key)
            before[key] = data if "unavailable" in data else data.get(field, [])
        return before

    def edit_tags(self, item_keys: list[str], add: list[str] = (), remove: list[str] = (),
                  source: str | None = None) -> dict:
        """Add and/or remove tags on items; each item's other tags stay as
        they are. Returns {"updated": {key: tags after}, "failed": {key: error}}."""
        keys, add, remove = _list_edit(item_keys, add, remove, "tag")
        return self._call(
            "tag edit", self._bridge.edit_tags, keys, add, remove,
            request={"keys": keys, "add": add, "remove": remove},
            before=self._list_before(keys, "tags"), source=source,
        )

    def edit_collections(self, item_keys: list[str], add: list[str] = (),
                         remove: list[str] = (), source: str | None = None) -> dict:
        """Add items to and/or remove them from collections; each item's other
        collections stay as they are. An unknown collection key changes
        nothing. Returns {"updated": {key: collection keys after}, "failed"}."""
        keys, add, remove = _list_edit(item_keys, add, remove, "collection key")
        return self._call(
            "collection edit", self._bridge.edit_collections, keys, add, remove,
            request={"keys": keys, "add": add, "remove": remove},
            before=self._list_before(keys, "collections"), source=source,
        )

    def create_note(self, parent_key: str, note: str, source: str | None = None) -> dict:
        """Add a child note to a regular item. Plain text is converted to
        HTML paragraphs (blank lines separate paragraphs); HTML is kept."""
        html_note = _note_html(note)
        return self._call(
            "note create", self._bridge.create_note, parent_key, html_note,
            request={"parent_key": parent_key, "note": html_note}, source=source,
        )

    def change_type(self, item_keys: list[str], item_type: str, dry_run: bool = False,
                    source: str | None = None) -> dict:
        """Change items' type (e.g. journalArticle -> conferencePaper). Zotero
        moves each field to its counterpart in the new type; fields with no
        place there are appended to Extra as "field: value" lines, so nothing
        is lost. dry_run reports those fields without changing anything."""
        keys = _clean_list(item_keys, "item key")
        if not keys:
            raise ZoteroWriteError("give at least one item key")
        if not isinstance(item_type, str) or not item_type.strip():
            raise ZoteroWriteError("give the new item type, e.g. 'conferencePaper'")
        item_type = item_type.strip()
        before = None if dry_run else {key: self._before(key) for key in keys}
        return self._call(
            "type change (dry run)" if dry_run else "type change",
            self._bridge.change_type, keys, item_type, dry_run,
            request={"keys": keys, "item_type": item_type, "dry_run": dry_run},
            before=before, source=source,
        )

    def restore_items(self, item_keys: list[str], source: str | None = None) -> dict:
        """Take items out of Zotero's trash."""
        keys = _clean_list(item_keys, "item key")
        if not keys:
            raise ZoteroWriteError("give at least one item key")
        return self._call("restore", self._bridge.restore_items, keys,
                          request={"keys": keys}, source=source)

    def delete_item(self, item_key: str, source: str | None = None) -> dict:
        """Move an item (of any type, including notes/annotations) to trash."""
        return self._call(
            f"delete of {item_key}", self._bridge.delete_item, item_key,
            request={"key": item_key}, before=self._before(item_key), source=source,
        )

    def create_collection(self, name: str, parent_key: str = "",
                          source: str | None = None) -> dict:
        """Create a collection, optionally under a parent collection."""
        name = _unescape_entities(name)
        return self._call(
            "collection create", self._bridge.create_collection, name, parent_key,
            request={"name": name, "parent_key": parent_key}, source=source,
        )

    def _collection_state(self, key: str) -> dict:
        """A collection's name, parent, subcollections and papers: the
        delete guard reads it, and the write log keeps it, so a deleted
        collection can be rebuilt by hand once Zotero's trash is emptied."""
        collections = self._reader.collections()
        found = next((c for c in collections if c["key"] == key), None)
        if found is None:
            raise ZoteroWriteError(f"no collection with key {key}")
        return {
            "key": key, "name": html.unescape(found["name"]), "parent": found["parent"],
            "subcollections": [c["key"] for c in collections if c["parent"] == key],
            "item_count": found["items"],
            "items": [i["key"] for i in self._reader.collection_items(key)],
        }

    def rename_collection(self, key: str, name: str, source: str | None = None) -> dict:
        """Rename a collection."""
        name = _unescape_entities(name).strip()
        if not name:
            raise ZoteroWriteError("a collection name cannot be empty")
        return self._call(
            "collection rename", self._bridge.rename_collection, key, name,
            request={"key": key, "name": name}, before=self._collection_state(key),
            source=source,
        )

    def delete_collection(self, key: str, force: bool = False,
                          source: str | None = None) -> dict:
        """Move a collection to Zotero's trash; its papers stay in the
        library. Without `force`, refuses a collection that holds papers or
        subcollections, and any venue folder."""
        state = self._collection_state(key)
        if not force:
            n_items = max(state["item_count"], len(state["items"]))
            if n_items or state["subcollections"]:
                raise ZoteroWriteError(
                    f"collection {state['name']!r} holds {n_items} items and"
                    f" {len(state['subcollections'])} subcollections; pass force to"
                    " delete it anyway (the items stay in the library)")
            if state["name"].startswith(VENUE_PREFIXES):
                raise ZoteroWriteError(
                    f"collection {state['name']!r} is a venue folder, the record of a"
                    " subscription decision; pass force to delete it anyway")
        return self._call(
            "collection delete", self._bridge.delete_collection, key,
            request={"key": key, "force": force}, before=state, source=source,
        )

    def move_collection(self, key: str, parent_key: str = "",
                        source: str | None = None) -> dict:
        """Move a collection under another (an empty parent_key: the top
        level). Refuses a parent that does not exist, and the collection
        itself or one of its descendants as parent."""
        state = self._collection_state(key)
        if parent_key:
            collections = self._reader.collections()
            parents = {c["key"]: c["parent"] for c in collections}
            if parent_key not in parents:
                raise ZoteroWriteError(f"no collection with key {parent_key}")
            ancestor = parent_key
            while ancestor:
                if ancestor == key:
                    raise ZoteroWriteError(
                        f"cannot move {state['name']!r} into itself or its own subcollection")
                ancestor = parents.get(ancestor, "")
        return self._call(
            "collection move", self._bridge.move_collection, key, parent_key,
            request={"key": key, "parent_key": parent_key}, before=state, source=source,
        )

    def link_attachment(self, parent_key: str, pdf_path: str, filename: str = "",
                        source: str | None = None) -> dict:
        """Attach a local PDF to an existing item as a linked file.

        Copies the file into the linked-attachment base directory (the source
        is left in place) and creates a linked_file attachment whose path is
        base-relative, so it stays valid on any machine sharing the base
        directory setting. When no filename is given, the file is named by
        the parent's citation key, falling back to the source name. If the
        parent already has an attachment with this file's content, nothing is
        written and that attachment is returned ("existing": true), so a
        repeated call is harmless.
        """
        pdf = Path(pdf_path).expanduser()
        if not pdf.is_file():
            raise ZoteroWriteError(f"no file at {pdf}")
        existing = self._attached_copy(parent_key, pdf)
        if existing:
            return existing
        if not filename:
            try:
                filename = (self._reader.item_raw(parent_key) or {}).get("citationKey", "")
            except Exception:  # noqa: BLE001 — Zotero closed or key unknown
                filename = ""
        raw_base = self._settings()["linked_attachments_dir"]
        if not raw_base:
            raise ZoteroWriteError("zotero.linked_attachments_dir is not set")
        base = Path(raw_base).expanduser()
        base.mkdir(parents=True, exist_ok=True)

        name = re.sub(r"[^\w.\- ]", "_", filename or pdf.name)
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        target = base / name
        stem, counter = target.stem, 1
        while target.exists() and not target.samefile(pdf):
            target = base / f"{stem}-{counter}.pdf"
            counter += 1
        copied = not target.exists()
        if copied:
            shutil.copy2(pdf, target)

        try:
            result = self._call(
                "attachment create",
                self._bridge.link_attachment,
                parent_key,
                str(target),
                title=target.name,
                request={"parent_key": parent_key, "source_file": str(pdf),
                         "file": str(target)},
                source=source,
            )
        except ZoteroWriteError as exc:
            # No attachment was created: remove the copy so a retry does not
            # leave an orphan (and a -1 suffix). After a timeout the record
            # may exist, so the file stays.
            if copied and not isinstance(exc.__cause__, BridgeTimeout):
                target.unlink(missing_ok=True)
            raise
        return {**result, "file": str(target)}

    def _attached_copy(self, parent_key: str, pdf: Path) -> dict | None:
        """The parent's attachment that already holds this file (same
        content, any name), so attaching it again changes nothing instead
        of creating a second record and a -1 copy. None if there is none or
        Zotero cannot be read (the bridge call then reports the problem)."""
        try:
            children = self._reader.children(parent_key)
        except Exception:  # noqa: BLE001 — Zotero closed or key unknown
            return None
        size, digest = pdf.stat().st_size, None
        for child in children:
            path = Path(child.get("path") or "")
            if (child.get("item_type") != "attachment" or not path.is_file()
                    or path.stat().st_size != size):
                continue
            digest = digest or _md5(pdf)
            if _md5(path) != digest:
                continue
            if (self._reader.item_raw(child["key"]) or {}).get("deleted"):
                continue  # in the trash: attach afresh
            return {"existing": True, "attachment_key": child["key"], "file": str(path)}
        return None
