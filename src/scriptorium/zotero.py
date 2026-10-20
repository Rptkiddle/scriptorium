"""Zotero reader: metadata ground truth via the local HTTP API.

All access goes through pyzotero in local mode (Web API v3 semantics served
by the running Zotero desktop app). This module is strictly read-only —
scriptorium's single source for what the library contains; writes live in
zotero_write (via the bridge plugin).

Requires the Zotero app to be running with "Allow other applications on this
computer to communicate with Zotero" enabled (Settings -> Advanced).
"""

import functools
import html
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pyzotero import zotero
from pyzotero.zotero_errors import PyZoteroError

from scriptorium.apis import norm_doi
from scriptorium.config import Config
from scriptorium.names import author_names, creator_keys

# itemTypes that are never papers
_NON_PAPER_TYPES = {"attachment", "note", "annotation"}


class ZoteroUnavailableError(RuntimeError):
    """Raised when the Zotero local API cannot be reached."""

    def __init__(self) -> None:
        super().__init__(
            "Zotero is not reachable on the local API. Start the Zotero app"
            " (and ensure Settings -> Advanced -> 'Allow other applications on"
            " this computer to communicate with Zotero' is enabled), then retry."
        )


@dataclass
class PaperRecord:
    """scriptorium's manifest entry for one Zotero item (metadata ground truth)."""

    item_key: str
    version: int
    item_type: str
    title: str
    creators: str  # e.g. "Trilling, Kiddle & Welbers"
    year: str
    doi: str
    venue: str
    citation_key: str  # native citationKey field (BBT-style), or "" if absent
    date_added: str
    pdf_key: str  # attachment item key, or "" if no PDF attachment
    pdf_path: str  # resolved local path, or "" if none
    pdf_status: str  # ok | not_downloaded | linked_missing | no_pdf
    abstract: str = ""
    tags: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)  # collection keys
    date_modified: str = ""  # Zotero dateModified, UTC ISO ("2026-09-25T02:05:10Z")
    extra: str = ""  # Zotero's Extra field (holds e.g. "arXiv: <id>" preprint lines)
    archive_id: str = ""  # Zotero's archiveID (e.g. "arXiv:2603.13270")
    # author name keys for filtering (names.creator_keys); empty: derive
    # from the formatted creators string
    creator_keys: list[str] = field(default_factory=list)
    # display names, "Given Surname" with role marks (names.author_names);
    # empty: derive from the formatted creators string
    authors: list[str] = field(default_factory=list)

    @property
    def citation(self) -> str:
        parts = [p for p in (self.creators, f"({self.year})" if self.year else "", self.title) if p]
        cite = " ".join(parts)
        return f"{cite}. {self.venue}." if self.venue else f"{cite}."

    def to_dict(self) -> dict:
        return asdict(self) | {"citation": self.citation}


def _format_creators(creators: list[dict]) -> str:
    names = []
    for c in creators:
        if last := c.get("lastName"):
            names.append(last)
        elif name := c.get("name"):
            names.append(name)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " & " + names[-1]


def _extract_year(data: dict) -> str:
    match = re.search(r"\b(\d{4})\b", data.get("date", ""))
    return match.group(1) if match else ""


def citation_key_of(data: dict) -> str:
    # Zotero 8+ native field; Extra-field convention (Better BibTeX pinned
    # keys) kept as a legacy fallback.
    if key := data.get("citationKey"):
        return key
    for line in data.get("extra", "").splitlines():
        if line.lower().startswith(("citation key:", "citationkey:")):
            return line.split(":", 1)[1].strip()
    return ""


def citekeys_of(items: list[dict]) -> set[str]:
    """The citation keys among item data dicts."""
    return {key for d in items if (key := citation_key_of(d))}


def dois_of(items: list[dict]) -> set[str]:
    """The normalised DOIs among item data dicts, from the DOI field or,
    for item types without one, a "DOI:" line in Extra."""
    dois = set()
    for d in items:
        if d.get("DOI"):
            dois.add(norm_doi(d["DOI"]))
        if m := re.search(r"DOI:\s*(\S+)", d.get("extra", ""), re.IGNORECASE):
            dois.add(norm_doi(m.group(1)))
    return dois


# Bumped whenever the derivation of indexed metadata from Zotero fields
# changes (venue, citation, creators, ...); a mismatch with the stamp in the
# sync state makes the next sync refresh every item's stored metadata.
METADATA_VERSION = 2  # 2: creator keys ignore case, accents and particles

# Zotero stores an item's venue under a type-specific field; first match
# wins, so a conference paper's proceedings title outranks its conference
# name, and a book section's book title outranks its publisher.
_VENUE_FIELDS = (
    "publicationTitle", "proceedingsTitle", "conferenceName", "meetingName",
    "bookTitle", "websiteTitle", "blogTitle", "forumTitle",
    "encyclopediaTitle", "dictionaryTitle", "programTitle",
    "publisher", "institution", "university", "repository",
)


def _venue(data: dict) -> str:
    for key in _VENUE_FIELDS:
        if value := data.get(key):
            return value
    return ""


def _locked(method):
    """Hold the reader's lock for the whole call. pyzotero keeps per-request
    state on the client (everything() pages by following self.links, which
    every request overwrites), so concurrent callers sharing one reader, as
    the daemon's threads do, must not interleave."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class ZoteroReader:
    """Read-only view of the Zotero library through the local API.

    Thread-safe: every method that touches the pyzotero client holds one
    re-entrant lock for its full duration."""

    def __init__(self, config: Config):
        self.config = config
        # Library ID 0 means "the local user library" on the local API.
        self._zot = zotero.Zotero(library_id="0", library_type="user", local=True)
        self._lock = threading.RLock()

    def _linked_base(self) -> Path | None:
        """The linked-attachment base directory from settings, or None."""
        from scriptorium.settings import load_settings

        raw = load_settings(self.config)["zotero"]["linked_attachments_dir"]
        return Path(raw).expanduser() if raw else None

    @_locked
    def check_available(self) -> None:
        """Raise ZoteroUnavailableError if the local API is not reachable."""
        try:
            self._zot.count_items()
        except (PyZoteroError, OSError) as exc:
            raise ZoteroUnavailableError() from exc

    @_locked
    def library_version(self) -> int:
        """Current library version (watermark for incremental sync)."""
        self.check_available()
        return int(self._zot.last_modified_version())

    @_locked
    def all_item_keys(self) -> set[str]:
        """Keys of all regular items currently in the library (for deletions)."""
        self.check_available()
        keys: set[str] = set()
        start = 0
        while True:
            batch = self._zot.items(
                itemType="-attachment", format="keys", limit=100, start=start
            )
            if isinstance(batch, bytes):
                batch = batch.decode()
            if isinstance(batch, str):
                batch = [k for k in batch.split() if k]
            if not batch:
                break
            keys.update(batch)
            if len(batch) < 100:
                break
            start += 100
        return keys

    @_locked
    def _pdf_attachments_by_parent(self) -> dict[str, dict]:
        """Map parent item key -> PDF attachment data.

        Items with several PDFs keep the one _choose_pdf picks.
        """
        attachments = self._zot.everything(self._zot.items(itemType="attachment"))
        grouped: dict[str, list[dict]] = {}
        for att in attachments:
            data = att["data"]
            if data.get("contentType") != "application/pdf":
                continue
            parent = data.get("parentItem")
            if parent:
                grouped.setdefault(parent, []).append(data)
        return {parent: self._choose_pdf(atts) for parent, atts in grouped.items()}

    def _choose_pdf(self, atts: list[dict]) -> dict:
        """Of an item's PDF attachments, the earliest-added whose file
        resolves (with a preprint/published pair usually the original
        import); if none resolves, the earliest. Ties break on attachment
        key, so every code path picks the same file."""
        ordered = sorted(atts, key=lambda a: (a.get("dateAdded", ""), a.get("key", "")))
        for att in ordered:
            if self._resolve_pdf(att)[2] == "ok":
                return att
        return ordered[0]

    def _resolve_pdf(self, att: dict | None) -> tuple[str, str, str]:
        """Return (pdf_key, pdf_path, pdf_status) for an attachment data dict."""
        if att is None:
            return "", "", "no_pdf"
        key = att["key"]
        link_mode = att.get("linkMode", "")
        if link_mode in {"imported_file", "imported_url"}:
            filename = att.get("filename") or ""
            path = self.config.zotero_data_dir / "storage" / key / filename
            if filename and path.exists():
                return key, str(path), "ok"
            return key, str(path) if filename else "", "not_downloaded"
        if link_mode == "linked_file":
            raw = att.get("path") or ""
            if raw.startswith("attachments:"):
                # Relative to the linked-attachment base directory.
                base = self._linked_base()
                if base is not None:
                    path = base / raw.removeprefix("attachments:")
                    if path.exists():
                        return key, str(path), "ok"
                return key, raw, "linked_missing"
            path = Path(raw).expanduser()
            if raw and path.exists():
                return key, str(path), "ok"
            return key, raw, "linked_missing"
        return key, "", "no_pdf"  # linked_url etc.

    def _make_record(self, data: dict, pdf_att: dict | None) -> PaperRecord:
        pdf_key, pdf_path, pdf_status = self._resolve_pdf(pdf_att)
        return PaperRecord(
            item_key=data["key"],
            version=data["version"],
            item_type=data.get("itemType", ""),
            title=data.get("title", ""),
            creators=_format_creators(data.get("creators", [])),
            year=_extract_year(data),
            doi=data.get("DOI", ""),
            venue=_venue(data),
            citation_key=citation_key_of(data),
            date_added=data.get("dateAdded", ""),
            pdf_key=pdf_key,
            pdf_path=pdf_path,
            pdf_status=pdf_status,
            abstract=data.get("abstractNote", ""),
            tags=[t["tag"] for t in data.get("tags", []) if t.get("tag")],
            collections=data.get("collections", []),
            date_modified=data.get("dateModified", ""),
            extra=data.get("extra", ""),
            archive_id=data.get("archiveID", ""),
            creator_keys=creator_keys(data.get("creators", [])),
            authors=author_names(data.get("creators", [])),
        )

    @_locked
    def iter_papers(self, since: int | None = None) -> list[PaperRecord]:
        """All regular items as PaperRecords (only those changed after `since`)."""
        self.check_available()
        params: dict = {"itemType": "-attachment"}
        if since is not None:
            params["since"] = since
        items = self._zot.everything(self._zot.items(**params))
        pdf_map = self._pdf_attachments_by_parent()
        return [
            self._make_record(item["data"], pdf_map.get(item["data"]["key"]))
            for item in items
            if item["data"].get("itemType") not in _NON_PAPER_TYPES
        ]

    @_locked
    def get_paper(self, item_key: str) -> PaperRecord | None:
        """One item by key, or None if not found / not a regular item."""
        self.check_available()
        try:
            item = self._zot.item(item_key)
        except PyZoteroError:
            return None
        data = item["data"]
        if data.get("itemType") in _NON_PAPER_TYPES:
            return None
        # Targeted fetch: only this item's children, not the whole library.
        pdf_atts = [
            child["data"]
            for child in self._zot.children(item_key)
            if child["data"].get("itemType") == "attachment"
            and child["data"].get("contentType") == "application/pdf"
        ]
        return self._make_record(data, self._choose_pdf(pdf_atts) if pdf_atts else None)

    # -- general catalogue reads (any item type, whole library) --------------

    @_locked
    def item_raw(self, item_key: str) -> dict | None:
        """One item's full data dict (any type), or None if not found."""
        self.check_available()
        try:
            return self._zot.item(item_key)["data"]
        except PyZoteroError:
            return None

    @_locked
    def library_snapshot(self) -> list[dict]:
        """The data dict of every top-level item: one full read of the
        library, for the derivations below (dois_of, citekeys_of)."""
        self.check_available()
        return [i["data"] for i in self._zot.everything(self._zot.top())]

    def citation_keys(self) -> set[str]:
        """Every citation key in use in the library."""
        return citekeys_of(self.library_snapshot())

    @_locked
    def children(self, item_key: str) -> list[dict]:
        """An item's child items (attachments, notes, annotations), trimmed
        to the fields a caller needs to inspect or delete them."""
        self.check_available()
        out = []
        for child in self._zot.children(item_key):
            data = child["data"]
            entry = {
                "key": data["key"],
                "item_type": data.get("itemType", ""),
                "date_added": data.get("dateAdded", ""),
            }
            if entry["item_type"] == "attachment":
                entry |= {
                    "link_mode": data.get("linkMode", ""),
                    "content_type": data.get("contentType", ""),
                    "title": data.get("title", ""),
                    "path": self._resolve_pdf(data)[1],
                }
            elif entry["item_type"] == "note":
                text = html.unescape(re.sub(r"<[^>]+>", " ", data.get("note", "")))
                entry["snippet"] = " ".join(text.split())[:300]
            elif entry["item_type"] == "annotation":
                entry |= {
                    "annotation_type": data.get("annotationType", ""),
                    "snippet": (data.get("annotationText") or data.get("annotationComment") or "")[:300],
                }
            out.append(entry)
        return out

    @_locked
    def collections(self) -> list[dict]:
        """All collections: key, name, parent key, direct item count."""
        self.check_available()
        return [
            {
                "key": c["data"]["key"],
                "name": c["data"]["name"],
                "parent": c["data"].get("parentCollection") or "",
                "items": c["meta"].get("numItems", 0),
            }
            for c in self._zot.everything(self._zot.collections())
        ]

    @_locked
    def collection_items(self, collection_key: str) -> list[dict]:
        """Regular items in one collection (not descendants): key, type, title."""
        self.check_available()
        items = self._zot.everything(
            self._zot.collection_items(collection_key, itemType="-attachment")
        )
        return [
            {
                "key": i["data"]["key"],
                "item_type": i["data"].get("itemType", ""),
                "title": i["data"].get("title", ""),
            }
            for i in items
            if i["data"].get("itemType") not in _NON_PAPER_TYPES
        ]

    @_locked
    def tags(self, contains: str = "") -> list[str]:
        """All tags in the library, optionally substring-filtered."""
        self.check_available()
        needle = contains.lower()
        return sorted(
            t for t in self._zot.everything(self._zot.tags()) if needle in t.lower()
        )
