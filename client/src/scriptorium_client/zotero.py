"""The member's Zotero, through the scriptorium-bridge plugin.

The plugin answers on Zotero's local server on this machine. It adds items
and reads which publications and PDFs the library holds; nothing here, and
nothing in the plugin, changes or removes what is in the library.
"""

from collections.abc import Callable

import httpx

from scriptorium_client.http import ServerError

ZOTERO = "http://127.0.0.1:23119"
COLLECTION = "Scriptorium"  # where added items land in the member's library
# Fields of a server item's details that describe the record it came from,
# not the publication.
_NOT_BIBLIOGRAPHIC = {"key", "version", "collections", "relations", "tags", "dateAdded",
                      "dateModified", "citationKey", "extra", "accessDate", "libraryCatalog"}


class ZoteroUnavailable(ServerError):
    """Zotero or the plugin does not answer; the message says which."""


def state(get: Callable = httpx.get, post: Callable = httpx.post) -> dict:
    """Whether Zotero runs on this machine and whether the scriptorium-bridge
    plugin answers in it."""
    def answers(call, url, **kwargs) -> bool:
        try:
            return call(url, timeout=3, **kwargs).status_code == 200
        except httpx.HTTPError:
            return False
    running = answers(get, f"{ZOTERO}/connector/ping")
    return {"zotero_running": running,
            "bridge_plugin": running and answers(post, f"{ZOTERO}/scriptorium/ping", json={})}


class Bridge:
    """The plugin's five functions."""

    def __init__(self, post: Callable = httpx.post, get: Callable = httpx.get):
        self._post, self._get = post, get

    def _call(self, function: str, body: dict | None = None, timeout: float = 600) -> dict:
        try:
            reply = self._post(f"{ZOTERO}/scriptorium/{function}", json=body or {},
                               timeout=timeout)
        except httpx.HTTPError:
            reply = None
        if reply is None or reply.status_code == 404:
            found = state(self._get, self._post)
            if not found["zotero_running"]:
                raise ZoteroUnavailable("Zotero is not running on this machine. Open Zotero,"
                                        " then ask again.")
            raise ZoteroUnavailable(
                "Zotero runs, but its scriptorium-bridge plugin does not answer. Install the"
                " plugin (version 0.2.0 or newer) in Zotero, then ask again.")
        if reply.status_code != 200:
            try:
                reason = reply.json().get("error", "")
            except ValueError:
                reason = reply.text[:200]
            raise ServerError(f"Zotero could not do this ({function}): {reason}")
        return reply.json()

    def identifiers(self) -> set[str]:
        """The DOIs and arXiv IDs of the library's items ("doi:...", "arxiv:...")."""
        return set(self._call("items/identifiers")["identifiers"])

    def create(self, items: list[dict]) -> dict:
        """{"created": {position: Zotero key}, "failed": {position: reason}}."""
        return self._call("items/create", {"items": items, "collection": COLLECTION})

    def import_pdf(self, parent_key: str, path: str) -> str:
        return self._call("attachments/import", {"parentKey": parent_key, "path": path})["created"]

    def pdfs(self, collections: list[str] | None = None) -> dict:
        """{"pdfs": [{"path", "identifiers"}], "without_identifier", "missing_file"}."""
        return self._call("attachments/pdfs", {"collections": collections or []})


def identifiers_of(item: dict) -> set[str]:
    """What a server item answers to: its own DOI and arXiv ID and those of
    its recorded preprints."""
    found = {i.lower() for i in item.get("preprints") or []}
    if item.get("doi"):
        found.add(f"doi:{item['doi'].lower()}")
    if item.get("arxiv_id"):
        found.add(f"arxiv:{item['arxiv_id'].lower()}")
    return found


def zotero_item(item: dict) -> dict:
    """A server item as the item JSON Zotero creates from: the publication's
    own fields, with its recorded arXiv preprints in Extra."""
    record = {k: v for k, v in (item.get("details") or {}).items()
              if k not in _NOT_BIBLIOGRAPHIC and v not in ("", None, [], {})}
    record.update(itemType=item["type"], title=item["title"], creators=item.get("creators") or [])
    for name, value in (("DOI", item.get("doi")), ("abstractNote", item.get("abstract")),
                        ("date", item.get("date"))):
        if value:
            record[name] = value
    if item.get("arxiv_id") and not record.get("archiveID"):
        record["archiveID"] = f"arXiv:{item['arxiv_id']}"
    preprints = [f"arXiv: {i.split(':', 1)[1]}" for i in item.get("preprints") or []
                 if i.lower().startswith("arxiv:")]
    if preprints:
        record["extra"] = "\n".join(preprints)
    return record
