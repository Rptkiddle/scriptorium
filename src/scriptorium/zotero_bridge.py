"""Client for the Scriptorium Bridge Zotero plugin.

The bridge (zotero-bridge/ in this repo) registers write endpoints on
Zotero's localhost server, so writes land in the local library instantly
and Zotero's own sync uploads them in the background. This is the hub's
only write path: when the bridge is unreachable (Zotero closed, or plugin
not installed) writes fail with a clear error.
"""

import json
import urllib.error
import urllib.request

BASE = "http://localhost:23119/scriptorium"


class BridgeUnavailable(RuntimeError):
    """The bridge cannot be reached: Zotero is closed or the plugin is
    not installed."""


class BridgeError(RuntimeError):
    """The bridge is reachable but the operation failed (bad key, bad
    data). Not a fallback case — the same data would fail on the web API."""


class BridgeTimeout(BridgeError):
    """The bridge accepted the request but did not answer in time. Zotero
    may still have completed the write: check before retrying, or the
    retry can create duplicates."""


class ZoteroBridge:
    def __init__(self, base: str = BASE, timeout: float = 60.0):
        self.base = base
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # Zotero up, plugin missing or too old
                raise BridgeUnavailable(
                    f"bridge endpoint {path} not registered (plugin missing, or older"
                    " than this hub needs)") from exc
            try:
                message = json.loads(exc.read()).get("error", str(exc))
            except Exception:  # noqa: BLE001 — any unparseable body
                message = str(exc)
            raise BridgeError(message) from exc
        except TimeoutError as exc:
            raise BridgeTimeout(
                f"no answer from Zotero within {self.timeout:.0f}s; the write may"
                " still have completed — check the library before retrying") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise BridgeTimeout(
                    f"no answer from Zotero within {self.timeout:.0f}s; the write may"
                    " still have completed — check the library before retrying") from exc
            raise BridgeUnavailable(str(exc)) from exc
        except OSError as exc:
            raise BridgeUnavailable(str(exc)) from exc

    def ping(self) -> dict:
        return self._post("ping", {})

    def create_items(self, items: list[dict]) -> dict:
        return self._post("items/create", {"items": items})

    def update_item(self, key: str, patch: dict) -> dict:
        return self._post("items/update", {"key": key, "patch": patch})

    def edit_tags(self, keys: list[str], add: list[str], remove: list[str]) -> dict:
        return self._post("items/tags", {"keys": keys, "add": add, "remove": remove})

    def edit_collections(self, keys: list[str], add: list[str], remove: list[str]) -> dict:
        return self._post("items/collections", {"keys": keys, "add": add, "remove": remove})

    def create_note(self, parent_key: str, note: str) -> dict:
        return self._post("notes/create", {"parentKey": parent_key, "note": note})

    def delete_item(self, key: str) -> dict:
        return self._post("items/delete", {"key": key})

    def link_attachment(self, parent_key: str, path: str, title: str = "") -> dict:
        return self._post(
            "attachments/link",
            {"parentKey": parent_key, "path": path, "title": title},
        )

    def create_collection(self, name: str, parent_key: str = "") -> dict:
        return self._post(
            "collections/create", {"name": name, "parentKey": parent_key}
        )

    def rename_collection(self, key: str, name: str) -> dict:
        return self._post("collections/update", {"key": key, "name": name})

    def delete_collection(self, key: str) -> dict:
        return self._post("collections/delete", {"key": key})

    def move_collection(self, key: str, parent_key: str = "") -> dict:
        return self._post("collections/move", {"key": key, "parentKey": parent_key})
