"""The tools the agent sees: one tool, one server request, same inputs.

The find tools share one `filters` object and may be confined to a saved
set with `within_set`. A result too large for a reply is saved whole to a
file on this machine (see results.fit); the set tools read, mark and export
such a file.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastmcp import FastMCP

from scriptorium_client import __version__, results
from scriptorium_client import settings as client_settings
from scriptorium_client.http import RateLimited, Server, ServerError

DONATE_BATCH = 20  # PDFs a folder donation sends per call; call again for the rest

ZOTERO = "http://127.0.0.1:23119"
INSTRUCTIONS = """Scriptorium is a shared collection of academic literature: items (publications \
with abstracts) and, for many, the full text in passages.

Pick the tool by the question:
- Is this paper held? (DOI, arXiv ID, exact title) -> lookup. An identifier not held is fetched \
from the registry and added.
- Everything matching filters (an author, a venue, years); how many -> list_items (count=0 for \
the number alone).
- Papers ABOUT a topic -> search_items. What papers SAY -> search_passages (full texts only).
- One item in full -> item. Values a filter can take -> facets. Entries for a .bib file -> bibtex.

Filters (one object, for all find tools): authors, authors_all, years, year_from, year_to, \
venues, publishers, categories, types, has_fulltext, added_since (YYYY-MM-DD), provenance. \
Values within one filter match any; different filters must all hold. Resolve venue, publisher \
and category names with facets first. The query holds the topic only; who, where and when are \
filters.

A large result is saved whole to a file and the reply gives its path, the count and the first \
rows: read on with set_read, narrow a later search to it with within_set, mark items with \
set_mark, export with set_export_bibtex.

PDFs: download_pdf saves a held PDF on this machine. donate_pdf shares the member's PDFs with \
all members (only on the member's request); donation_progress shows what became of them."""


def zotero_state(get=httpx.get, post=httpx.post) -> dict:
    """Whether Zotero runs on this machine and whether the Scriptorium
    Bridge plugin answers in it."""
    def answers(call, url, **kwargs) -> bool:
        try:
            return call(url, timeout=3, **kwargs).status_code == 200
        except httpx.HTTPError:
            return False
    running = answers(get, f"{ZOTERO}/connector/ping")
    return {"zotero_running": running,
            "bridge_plugin": running and answers(post, f"{ZOTERO}/scriptorium/ping", json={})}


class Tools:
    """The tools, as methods; build_server() registers them."""

    def __init__(self, settings: dict | None = None, server: Server | None = None,
                 zotero=zotero_state):
        self.settings = settings if settings is not None else client_settings.load()
        self.server = server or Server(self.settings)
        self._zotero = zotero

    # -- helpers -------------------------------------------------------------------

    def _filters(self, filters: dict | None, within_set: str | None) -> dict | None:
        filters = dict(filters or {})
        if within_set:
            filters["item_keys"] = results.set_keys(within_set)
        return filters or None

    def _fit(self, reply: dict, list_key: str, name: str, request: dict) -> dict:
        return results.fit(reply, list_key, int(self.settings["reply_chars"]),
                           self.settings["results_dir"], name, request)

    # -- find ----------------------------------------------------------------------

    def lookup(self, inputs: list[str]) -> dict:
        """Exact lookup of DOIs, arXiv IDs (or their links) and exact titles,
        one or many at once. An identifier finds the item that answers to it,
        including the published version of a preprint. An identifier the
        collection does not hold is fetched from the registry (Crossref,
        DataCite), verified and added. Returns one result per input:
        found, items, and a note where something needs saying."""
        return self.server.lookup(inputs)

    def list_items(self, filters: dict | None = None, count: int = 20, offset: int = 0,
                   order: str = "added", fields: list[str] | None = None,
                   within_set: str | None = None) -> dict:
        """Every item matching the filters, with the total. No topic: for
        that use search_items. count=0 returns the total alone. order:
        added (newest first), year, title. fields: any of key,
        citation_key, type, title, authors, year, date, venue, publisher,
        category, doi, arxiv_id, abstract, has_fulltext, provenance,
        added_at, preprints, details (default: the first nine without date).
        within_set: the path of a saved result, to list only its items."""
        request = {"filters": self._filters(filters, within_set), "count": count,
                   "offset": offset, "order": order, "fields": fields}
        return self._fit(self.server.items(**request), "items", "items", request)

    def search_items(self, query: str, filters: dict | None = None, count: int = 20,
                     fields: list[str] | None = None, phrase: str | None = None,
                     within_set: str | None = None) -> dict:
        """Items ABOUT a topic: nearest by meaning of title and abstract,
        within the filters, best first, each with a score. Add "abstract" to
        fields to judge relevance without opening items. phrase: an exact
        phrase the title or abstract must also hold. within_set: the path of
        a saved result, to search only its items (then exact, not approximate)."""
        request = {"query": query, "filters": self._filters(filters, within_set),
                   "count": count, "fields": fields, "phrase": phrase}
        return self._fit(self.server.search_items(**request), "items", f"search {query}", request)

    def search_passages(self, query: str, filters: dict | None = None, count: int = 10,
                        phrase: str | None = None, within_set: str | None = None) -> dict:
        """What papers SAY: paragraphs of held full texts nearest the query
        by meaning, within the item filters, best first. Each passage comes
        with its text, pages, part (body, footnote, appendix) and the item it
        is from. Only items with full text are searched (has_fulltext).
        phrase: an exact phrase the passage must also hold."""
        request = {"query": query, "filters": self._filters(filters, within_set),
                   "count": count, "phrase": phrase}
        return self._fit(self.server.search_passages(**request), "passages",
                         f"passages {query}", request)

    # -- inspect -------------------------------------------------------------------

    def item(self, key: str) -> dict:
        """One item in full, by item key or citation key: all bibliographic
        fields, abstract, creators, recorded preprints, how many passages it
        has and whether its PDF is held."""
        return self.server.item(key)

    def facets(self, field: str, filters: dict | None = None, contains: str = "",
               count: int = 50) -> dict:
        """The values a filter can take, with how many items have each, most
        frequent first. field: year, venue, publisher, category, type,
        provenance, has_fulltext, author. contains narrows by a part of the
        name; filters counts within a set of items."""
        return self.server.facets(field=field, filters=filters, contains=contains, count=count)

    def bibtex(self, keys: list[str], format: str = "biblatex", abstracts: bool = False) -> dict:
        """BibLaTeX (default) or BibTeX entries for items, by item or
        citation keys, for a manuscript's .bib file. Returns the text, the
        number of entries and the keys not held."""
        return self.server.bibtex(keys, format, abstracts)

    def status(self) -> dict:
        """Whether everything is in place: the server reachable and the size
        of its collection, this client's version, and whether Zotero and its
        Scriptorium Bridge plugin answer on this machine."""
        state = {"client_version": __version__, "server_address": self.server.address or None}
        try:
            health = self.server.health()
            state.update(server_reachable=True, items=health["items"],
                         fulltexts=health["fulltexts"], server_version=health["server"])
        except ServerError as exc:
            state.update(server_reachable=False, server_problem=str(exc))
        return {**state, **self._zotero()}

    # -- files ---------------------------------------------------------------------

    def download_pdf(self, key: str) -> dict:
        """Save the held PDF of an item (item or citation key) on this
        machine, in the client's PDF folder; returns its path. Not every
        item with full text has its PDF held."""
        data, name, sha256 = self.server.download(key)
        if sha256 and hashlib.sha256(data).hexdigest() != sha256:
            raise ServerError(f"the PDF of {key} arrived damaged; try again")
        folder = Path(self.settings["pdf_dir"]).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / Path(name).name
        path.write_bytes(data)
        return {"saved_to": str(path), "bytes": len(data)}

    def _ledger(self) -> Path:
        return Path(self.settings["results_dir"]).expanduser().parent / "donations.jsonl"

    def _sent(self) -> dict[str, str]:
        """MD5 -> "sent" or "refused", for every PDF this machine has offered."""
        ledger = self._ledger()
        if not ledger.exists():
            return {}
        entries = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
        return {e["md5"]: ("refused" if e.get("refused") else "sent") for e in entries}

    def _md5s(self, files: list[Path]) -> dict[Path, str]:
        """Each file's MD5, read once: kept in hashes.json beside the ledger
        by path, size and modification time, so a large folder is not read
        again on every call."""
        cache_path = self._ledger().with_name("hashes.json")
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        out = {}
        for f in files:
            stat = f.stat()
            stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
            entry = cache.get(str(f))
            if not entry or entry[0] != stamp:
                entry = [stamp, hashlib.md5(f.read_bytes()).hexdigest()]
                cache[str(f)] = entry
            out[f] = entry[1]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache))
        return out

    def donate_pdf(self, path: str, identifier: str | None = None) -> dict:
        """Share PDFs with all members: one PDF (with its DOI or arXiv ID
        when known), or every PDF in a folder and its subfolders. Only on
        the member's own request: donated PDFs are shared with all members.
        Only the file and its identifier are sent; the server removes
        annotations and file details, checks that the PDF is the publication
        named (else reads the DOI on its first page), and discards what it
        cannot match. PDFs the server holds already, and ones this machine
        has sent before, are skipped. A folder is sent in batches: call
        again until "remaining" is 0. Follow up with donation_progress."""
        source = Path(path).expanduser()
        if not source.exists():
            raise ServerError(f"{source} does not exist")
        files = [source] if source.is_file() else sorted(
            p for p in source.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
        held = self.server.held()
        held_md5 = {m for i in held["items"] for m in i.get("md5", [])}
        held_ids = {(i.get("doi") or "").lower() for i in held["items"]} - {""}
        sent = self._sent()
        report = {"pdfs": len(files), "sent": [], "refused": [], "skipped_held": 0,
                  "skipped_sent_before": 0, "skipped_refused_before": 0, "remaining": 0}
        if identifier and identifier.lower().removeprefix("https://doi.org/") in held_ids:
            report["skipped_held"] = len(files)
            return report
        todo = []
        for f, md5 in self._md5s(files).items():
            if md5 in held_md5:
                report["skipped_held"] += 1
            elif sent.get(md5) == "sent":
                report["skipped_sent_before"] += 1
            elif sent.get(md5) == "refused":
                report["skipped_refused_before"] += 1
            else:
                todo.append((f, md5))
        ledger = self._ledger()
        ledger.parent.mkdir(parents=True, exist_ok=True)
        tried = 0
        for f, md5 in todo[:DONATE_BATCH]:
            entry = {"md5": md5, "file": str(f), "at": datetime.now(UTC).isoformat(timespec="seconds")}
            try:
                reply = self.server.donate(f.read_bytes(), identifier if source.is_file() else None)
            except RateLimited as exc:
                report["note"] = f"{exc} Call donate_pdf again in a minute to send the rest."
                break
            except ServerError as exc:
                if exc.status not in (400, 413):  # not about this file: stop, nothing recorded
                    raise
                entry["refused"] = str(exc)
                report["refused"].append({"file": f.name, "reason": str(exc)})
            else:
                entry["donation"] = reply["donation"]
                report["sent"].append({"file": f.name, "donation": reply["donation"]})
            tried += 1
            with open(ledger, "a") as log:
                log.write(json.dumps(entry) + "\n")
        report["remaining"] = len(todo) - tried
        if report["remaining"] and "note" not in report:
            report["note"] = "Call donate_pdf again with the same folder to send the rest."
        return report

    def donation_progress(self, count: int = 50, offset: int = 0) -> dict:
        """The member's own donations, newest first: waiting, processing,
        stored, already held (another copy was there first), rejected (with
        the reason, e.g. not the publication named) or failed (the server's
        administrator can run it again)."""
        return self.server.donations(count, offset)

    # -- sets ----------------------------------------------------------------------

    def set_read(self, path: str, page: int = 1, page_size: int = 20) -> dict:
        """A page of a saved result (a set), each row with its mark."""
        return results.set_read(path, page, page_size)

    def set_mark(self, path: str, keys: list[str], included: bool) -> dict:
        """Mark items of a saved result as included (true) or excluded
        (false), e.g. while screening. Once any item is marked included, the
        set stands for its included items; before that, for all not excluded."""
        return results.set_mark(path, keys, included)

    def set_export_bibtex(self, path: str, format: str = "biblatex") -> dict:
        """Write the set's items as a .bib file beside it; returns the file's
        path and the number of entries."""
        keys = results.set_keys(path)
        text, missing = [], []
        for start in range(0, len(keys), 200):
            reply = self.server.bibtex(keys[start:start + 200], format)
            text.append(reply["bibtex"])
            missing += reply["missing"]
        out = Path(path).expanduser().with_suffix(".bib")
        out.write_text("\n".join(t for t in text if t))
        return {"saved_to": str(out), "entries": len(keys) - len(missing), "missing": missing}


TOOL_NAMES = ("lookup", "list_items", "search_items", "search_passages", "item", "facets",
              "bibtex", "status", "set_read", "set_mark", "set_export_bibtex",
              "download_pdf", "donate_pdf", "donation_progress")


def agent_name() -> str:
    """The name the connected agent gave when it opened the MCP session
    ("claude-code", "cursor", ...); "" when it cannot be read."""
    try:
        from fastmcp.server.dependencies import get_context

        params = get_context().session.client_params
        info = getattr(params, "client_info", None) or params.clientInfo
        return f"{info.name} {info.version or ''}".strip()
    except Exception:  # noqa: BLE001 — outside a session, or a library change: no name
        return ""


def build_server(tools: Tools | None = None) -> FastMCP:
    """The MCP server with every tool registered. Each call first notes
    which agent is asking, so the server's usage log can record it."""
    import functools

    tools = tools or Tools()
    mcp = FastMCP("scriptorium", instructions=INSTRUCTIONS)
    for name in TOOL_NAMES:
        method = getattr(tools, name)

        @functools.wraps(method)
        def tool(*args, _method=method, **kwargs):
            tools.server.agent = agent_name() or tools.server.agent
            return _method(*args, **kwargs)

        del tool.__wrapped__  # the tool's inputs are the method's, as wraps() copied them
        tool.__signature__ = __import__("inspect").signature(method)
        mcp.add_tool(tool)
    return mcp
