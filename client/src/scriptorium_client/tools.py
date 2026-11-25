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
from scriptorium_client import zotero as zotero_library
from scriptorium_client import notice as member_notice
from scriptorium_client.notice import CANNOT_ASK, DIALOG, NOT_AGREED
from scriptorium_client import settings as client_settings
from scriptorium_client.http import RateLimited, Server, ServerError

UPLOAD_BATCH = 20  # PDFs an upload sends per call; call again for the rest
COLLECTION_BATCH = 500  # item keys one request carries to a collection
ADD_BATCH = 25  # items one call adds to Zotero; call again for the rest

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
venues, publishers, categories, types, has_fulltext, added_since (YYYY-MM-DD), provenance, \
collections (the member's own, by number or name). \
Values within one filter match any; different filters must all hold. Resolve venue, publisher \
and category names with facets first. The query holds the topic only; who, where and when are \
filters.

A large result is saved whole to a file and the reply gives its path, the count and the first \
rows: read on with set_read, narrow a later search to it with within_set, mark items with \
set_mark, export with set_export_bibtex.

Collections are the member's own lists of items, kept on the server and seen by no one else. \
An active collection is named by the member: collection_add files items under a name (a new \
name makes a new collection), which is how an item is labelled to find it again. A history is \
the record of a workflow run (what was asked and what came back, ranked), made with \
history_record at the end of a search, discovery or interrogation workflow. collections lists \
them, collection shows one with its items, collection_remove takes items out, collection_edit \
renames, keeps a history as an active collection, or deletes. item shows which of the member's \
collections hold an item.

PDFs: pdf_download saves a held PDF on this machine. pdf_upload shares the member's PDFs with \
all members (only on the member's request); pdf_upload_progress shows what became of them.

Zotero (when the member runs Zotero with the scriptorium-bridge plugin; status says so): \
zotero_add_items puts chosen items, with their PDFs where held, into the member's library; \
zotero_pdf_upload donates PDFs from it. Neither changes or removes anything in the library."""

ADMIN_INSTRUCTIONS = """

The admin menu (this member is an admin of the server): start with admin_overview, the state of \
the server on one page. admin_settings shows and changes settings; admin_members, admin_venues, \
admin_runs (harvests and backups) and admin_jobs (PDF uploads) list first and act on request. A \
call that would change something changes nothing the first time: it returns a preview. Show the \
preview to the admin, and only after their yes call again with confirm=true. A new member's key \
is shown once: give it to the admin in full and do not repeat it afterwards."""


class Tools:
    """The tools, as methods; build_server() registers them."""

    def __init__(self, settings: dict | None = None, server: Server | None = None,
                 zotero=zotero_library.state,
                 bridge: zotero_library.Bridge | None = None):
        self.settings = settings if settings is not None else client_settings.load()
        self.server = server or Server(self.settings)
        self._zotero = zotero
        self.bridge = bridge or zotero_library.Bridge()

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
        """Whether everything is in place: the server reachable, the size of
        its collection and whether it hands out PDFs and accepts donated
        ones, this client's version, whether the member is signed in with a
        login or an API key, and whether
        Zotero and its Scriptorium Bridge plugin answer on this machine."""
        state = {"client_version": __version__, "server_address": self.server.address or None}
        try:
            health = self.server.health()
            state.update(server_reachable=True, items=health["items"],
                         fulltexts=health["fulltexts"], server_version=health["server"],
                         pdf_download=health.get("pdf_download", True),
                         pdf_upload=health.get("pdf_upload", True))
            if health.get("admin"):
                state.update(admin=True)
            state.update(signed_in_with="login" if self.server.session else "API key")
        except ServerError as exc:
            state.update(server_reachable=False, server_problem=str(exc))
        return {**state, **self._zotero()}

    # -- files ---------------------------------------------------------------------

    def pdf_download(self, key: str) -> dict:
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
        folder = Path(self.settings["results_dir"]).expanduser().parent
        earlier = folder / "donations.jsonl"  # the ledger's name before client 0.3.0
        if earlier.exists() and not (folder / "pdf_uploads.jsonl").exists():
            earlier.rename(folder / "pdf_uploads.jsonl")
        return folder / "pdf_uploads.jsonl"

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

    def pdf_upload(self, path: str, identifier: str | None = None) -> dict:
        """Share PDFs with all members: one PDF (with its DOI or arXiv ID
        when known), or every PDF in a folder and its subfolders. Only on
        the member's own request: donated PDFs are shared with all members.
        Only the file and its identifier are sent; the server removes
        annotations and file details, checks that the PDF is the publication
        named (else reads the DOI on its first page), and discards what it
        cannot match. PDFs the server holds already, and ones this machine
        has sent before, are skipped. A folder is sent in batches: call
        again until "remaining" is 0. Follow up with pdf_upload_progress."""
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
        self._send(todo, report, "pdf_upload", lambda f: identifier if source.is_file() else None)
        return report

    def _send(self, todo: list[tuple[Path, str]], report: dict, tool: str, identifier_of) -> None:
        """Upload the first batch of `todo` ((file, MD5) pairs), recording each
        file sent or refused in the ledger, and note in the report what is
        left. A refusal that is not about the file stops the batch with
        nothing recorded."""
        ledger = self._ledger()
        ledger.parent.mkdir(parents=True, exist_ok=True)
        tried = 0
        for f, md5 in todo[:UPLOAD_BATCH]:
            entry = {"md5": md5, "file": str(f), "at": datetime.now(UTC).isoformat(timespec="seconds")}
            try:
                reply = self.server.pdf_upload(f.read_bytes(), identifier_of(f))
            except RateLimited as exc:
                report["note"] = f"{exc} Call {tool} again in a minute to send the rest."
                break
            except ServerError as exc:
                if exc.status not in (400, 413):  # not about this file: stop, nothing recorded
                    raise
                entry["refused"] = str(exc)
                report["refused"].append({"file": f.name, "reason": str(exc)})
            else:
                entry["upload"] = reply["upload"]
                report["sent"].append({"file": f.name, "upload": reply["upload"]})
            tried += 1
            with open(ledger, "a") as log:
                log.write(json.dumps(entry) + "\n")
        report["remaining"] = len(todo) - tried
        if report["remaining"] and "note" not in report:
            report["note"] = f"Call {tool} again with the same inputs to send the rest."

    def pdf_upload_progress(self, count: int = 50, offset: int = 0) -> dict:
        """The member's own donations, newest first: waiting, processing,
        stored, already held (another copy was there first), rejected (with
        the reason, e.g. not the publication named) or failed (the server's
        administrator can run it again)."""
        return self.server.pdf_uploads(count, offset)

    # -- Zotero --------------------------------------------------------------------

    def _added(self) -> Path:
        return self._ledger().with_name("zotero_added.jsonl")

    def zotero_add_items(self, keys: list[str] | None = None, from_set: str | None = None,
                         pdfs: bool = True) -> dict:
        """Add items of the collection to the member's Zotero library, on the
        member's request: the items named by key, or those of a saved result
        (from_set, its path). Needs Zotero running with the scriptorium-bridge
        plugin. Publications the library holds already (by DOI or arXiv ID)
        are skipped; new items land in the Zotero collection "Scriptorium",
        with their PDF where the server holds one and hands PDFs out (pdfs=
        false leaves the PDFs). Nothing in the library is changed or removed.
        At most 25 items are added per call: call again with the same inputs
        until "remaining" is 0."""
        wanted = list(dict.fromkeys((keys or []) + (results.set_keys(from_set) if from_set else [])))
        if not wanted:
            raise ServerError("name the items to add: keys, or from_set")
        held = self.bridge.identifiers()  # first: without Zotero nothing else is done
        added_path = self._added()
        before = {json.loads(line)["key"] for line in added_path.read_text().splitlines()
                  if line.strip()} if added_path.exists() else set()
        with_pdfs = pdfs and self.status().get("pdf_download", False)
        report = {"asked": len(wanted), "added": [], "already_in_zotero": 0, "pdfs_attached": 0,
                  "failed": [], "remaining": 0}
        todo = []
        for position, key in enumerate(wanted):
            if len(todo) == ADD_BATCH:
                report["remaining"] = len(wanted) - position
                report["note"] = "Call zotero_add_items again with the same inputs to add the rest."
                break
            try:
                item = self.server.item(key)
            except RateLimited as exc:
                report["remaining"] = len(wanted) - position
                report["note"] = f"{exc} Call zotero_add_items again in a minute to add the rest."
                break
            except ServerError as exc:
                if exc.status != 404:
                    raise
                report["failed"].append({"key": key, "reason": "no such item in the collection"})
                continue
            names = zotero_library.identifiers_of(item)
            if names & held or (not names and item["key"] in before):
                report["already_in_zotero"] += 1
                continue
            held |= names  # the same publication twice in one request is added once
            todo.append(item)
        if todo:
            reply = self.bridge.create([zotero_library.zotero_item(i) for i in todo])
            added_path.parent.mkdir(parents=True, exist_ok=True)
            for position, item in enumerate(todo):
                zotero_key = reply["created"].get(str(position))
                if not zotero_key:
                    report["failed"].append({"key": item["key"], "reason":
                                             reply["failed"].get(str(position), "not created")})
                    continue
                with open(added_path, "a") as log:
                    log.write(json.dumps({"key": item["key"], "zotero": zotero_key, "at":
                                          datetime.now(UTC).isoformat(timespec="seconds")}) + "\n")
                entry = {"key": item["key"], "zotero": zotero_key, "title": item["title"]}
                if with_pdfs and item.get("pdf_held"):
                    try:
                        saved = Path(self.pdf_download(item["key"])["saved_to"])
                        self.bridge.import_pdf(zotero_key, str(saved))
                        saved.unlink(missing_ok=True)  # Zotero keeps its own copy
                        report["pdfs_attached"] += 1
                    except ServerError as exc:
                        entry["pdf"] = f"not attached: {exc}"
                report["added"].append(entry)
        if pdfs and not with_pdfs:
            report["pdfs"] = "this server does not hand out PDFs at present; items were added without"
        return report

    def zotero_pdf_upload(self, collections: list[str] | None = None,
                          confirm: bool = False) -> dict:
        """Donate PDFs from the member's Zotero library: those of the named
        Zotero collections (with their subcollections), or of the whole
        library. Only on the member's own request: donated PDFs are shared
        with all members. Needs Zotero running with the scriptorium-bridge
        plugin. Only PDFs whose item has a DOI or arXiv ID are offered, and
        only the PDF and that identifier are sent: no notes, tags or
        annotations (the server removes annotations from the file). What the
        server holds already is worked out on this machine. Without confirm
        nothing is sent: the reply says how many PDFs would go. Tell the
        member that number, and call again with confirm=true once they
        agree; then at most 20 are sent per call, so call again until
        "remaining" is 0. Follow up with pdf_upload_progress."""
        found = self.bridge.pdfs(collections)  # first: without Zotero nothing else is done
        if not self.status().get("pdf_upload", False):
            raise ServerError("this server does not accept donated PDFs at present")
        held = self.server.held()
        held_md5 = {m for i in held["items"] for m in i.get("md5", [])}
        held_ids = ({f"doi:{i['doi'].lower()}" for i in held["items"] if i.get("doi")}
                    | {f"arxiv:{i['arxiv_id'].lower()}" for i in held["items"] if i.get("arxiv_id")})
        report = {"pdfs": len(found["pdfs"]), "without_identifier": found["without_identifier"],
                  "missing_file": found["missing_file"], "skipped_held": 0,
                  "skipped_sent_before": 0, "skipped_refused_before": 0,
                  "sent": [], "refused": [], "remaining": 0}
        offered = {}
        for pdf in found["pdfs"]:
            if set(pdf["identifiers"]) & held_ids:
                report["skipped_held"] += 1
            elif Path(pdf["path"]).is_file():
                offered[Path(pdf["path"])] = sorted(pdf["identifiers"], key=lambda i: not i.startswith("doi:"))[0]
            else:
                report["missing_file"] += 1
        sent = self._sent()
        todo = []
        for f, md5 in self._md5s(list(offered)).items():
            if md5 in held_md5:
                report["skipped_held"] += 1
            elif sent.get(md5) == "sent":
                report["skipped_sent_before"] += 1
            elif sent.get(md5) == "refused":
                report["skipped_refused_before"] += 1
            else:
                todo.append((f, md5))
        if not confirm:
            report["remaining"] = len(todo)
            report["note"] = (f"{len(todo)} PDFs would be sent. Nothing was sent: tell the member,"
                              " and call again with confirm=true once they agree.")
            return report

        def identifier(f: Path) -> str:
            kind, _, value = offered[f].partition(":")
            return value if kind == "doi" else f"arXiv:{value}"
        self._send(todo, report, "zotero_pdf_upload", identifier)
        return report

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


    # -- collections -----------------------------------------------------------------

    def _keys(self, keys: list[str] | None, from_set: str | None) -> list[str]:
        """The item keys given, and those of a saved result when one is named."""
        return list(dict.fromkeys([*(keys or []), *(results.set_keys(from_set) if from_set else [])]))

    def collections(self, kind: str | None = None, contains: str = "", count: int = 50,
                    offset: int = 0) -> dict:
        """The member's own collections, newest first, each with its number,
        name and how many items it holds. kind: active (named by the
        member) or history (recorded by a workflow run, with what was
        asked); none for both. contains narrows by a part of the name or of
        what was asked."""
        return self.server.collections("list", kind=kind, contains=contains or None,
                                       count=count, offset=offset or None)

    def collection(self, collection: str, count: int = 50, offset: int = 0,
                   fields: list[str] | None = None) -> dict:
        """One of the member's collections with its items: a history in the
        order of its result (each item with its rank), an active collection
        in the order the items were added. collection: its number, or an
        active collection's name. fields as in list_items. To search or
        filter inside a collection, give the find tools the filter
        collections instead."""
        request = {"collection": str(collection), "count": count, "offset": offset or None,
                   "fields": fields}
        return self._fit(self.server.collections("show", **request), "items",
                         f"collection {collection}", request)

    def collection_add(self, collection: str, keys: list[str] | None = None,
                       from_set: str | None = None, from_collection: str | None = None) -> dict:
        """File items under a name so the member finds them again: add them
        to an active collection, by its number or name. A name the member
        has no collection of makes a new one. keys: item or citation keys.
        from_set: the path of a saved result, to add its items (the
        included ones, when items were marked). from_collection: another of
        the member's collections, to add all its items. Items it holds
        already are skipped."""
        todo = self._keys(keys, from_set)
        reply = self.server.collections("add", collection=str(collection),
                                        keys=todo[:COLLECTION_BATCH], from_collection=from_collection)
        new, added, not_held = reply["new_collection"], reply["added"], reply["not_held"]
        for start in range(COLLECTION_BATCH, len(todo), COLLECTION_BATCH):
            reply = self.server.collections("add", collection=str(reply["collection"]),
                                            keys=todo[start:start + COLLECTION_BATCH])
            added, not_held = added + reply["added"], not_held + reply["not_held"]
        return {**reply, "new_collection": new, "added": added, "not_held": not_held}

    def collection_remove(self, collection: str, keys: list[str]) -> dict:
        """Take items (item or citation keys) out of one of the member's
        collections, named by its number or name. The items stay in
        Scriptorium."""
        return self.server.collections("remove", collection=str(collection), keys=keys)

    def collection_edit(self, collection: str, name: str | None = None, keep: bool = False,
                        delete: bool = False) -> dict:
        """Change one of the member's collections, named by its number or
        name. name: rename it. keep=true: turn a history into an active
        collection (under name, when given). delete=true: delete the
        collection, of either kind, on the member's request; its items
        stay in Scriptorium and the collection cannot be brought back."""
        return self.server.collections("edit", collection=str(collection), name=name,
                                       keep=keep or None, delete=delete or None)

    def history_record(self, workflow: str, name: str, keys: list[str] | None = None,
                       request: dict | None = None, from_set: str | None = None) -> dict:
        """Record a finished workflow run in the member's history: the
        workflow (search, discovery, interrogation), a short name for the
        run (its question), what was asked (request: query, filters,
        question) and the items it found, best first (keys, or from_set:
        the path of a saved result). Called once at the end of a workflow;
        the member sees the run under collections with kind history."""
        todo = self._keys(keys, from_set)
        reply = self.server.collections("record", workflow=workflow, name=name, request=request,
                                        keys=todo[:COLLECTION_BATCH])
        added, not_held = reply["added"], reply["not_held"]
        for start in range(COLLECTION_BATCH, len(todo), COLLECTION_BATCH):
            reply = self.server.collections("record", collection=str(reply["collection"]),
                                            keys=todo[start:start + COLLECTION_BATCH])
            added, not_held = added + reply["added"], not_held + reply["not_held"]
        return {**reply, "added": added, "not_held": not_held}

    # -- the admin menu ------------------------------------------------------------
    # Registered only when the server says this member is an admin.

    def _admin_note(self) -> Path:
        return self._ledger().with_name("admin.json")

    def admin_known(self) -> bool:
        """Whether the admin menu is shown from the start of this session:
        the client's note from an earlier session says the server counts
        the member as an admin, or the setting `admin` is "yes". Nothing is
        asked of the server here: no request leaves before the member has
        accepted the notice."""
        if str(self.settings.get("admin", "")).lower() == "yes":
            return True
        try:
            note = json.loads(self._admin_note().read_text())
        except (OSError, ValueError):
            return False
        return bool(note.get("admin")) and note.get("server") == self.server.address

    def learn_admin(self) -> bool:
        """Ask the server whether the member is an admin and keep the
        answer for the next session; False when it cannot be asked."""
        try:
            admin = bool(self.server.health().get("admin"))
        except (ServerError, ValueError):
            return False
        try:
            note = self._admin_note()
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(json.dumps({"server": self.server.address, "admin": admin}))
        except OSError:  # an agent app that keeps the client from its files
            pass
        return admin

    def admin_overview(self) -> dict:
        """ADMIN. The state of the server on one page: the collection's
        size and what waits for a vector, PDF uploads waiting and failed,
        the next scheduled harvest and backup and how the last ones went,
        members and requests, disk space, the settings that differ from
        their defaults, and the latest admin changes. Start here."""
        return self.server.admin_overview()

    def admin_settings(self, name: str | None = None, value: str | None = None,
                       confirm: bool = False) -> dict:
        """ADMIN. The server's settings. No inputs: every setting with its
        value, default, what it does, what a value must be, when a change
        takes effect and whether it can be changed here. name: that
        setting. name and value: a preview of the change; it is made only
        with confirm=true, after the admin's yes. Keys for outside sources
        and the places of the server's parts are changed on the host only."""
        return self.server.admin("settings", name=name, value=value, confirm=confirm or None)

    def admin_members(self, action: str = "list", name: str | None = None,
                      confirm: bool = False) -> dict:
        """ADMIN. Members. action list: every member with status, date
        added, last request, requests in all and in the last seven days,
        and PDFs uploaded. add (name): a new member; their API key is in
        the reply once and stored nowhere. revoke (name): the member's key
        is refused from now on. add and revoke return a preview first and
        act only with confirm=true, after the admin's yes."""
        return self.server.admin("members", action=action, name=name, confirm=confirm or None)

    def admin_venues(self, action: str = "list", name: str | None = None, contains: str = "",
                     source_id: str | None = None, issns: list[str] | None = None,
                     publisher: str | None = None, category: str | None = None,
                     needs_filter: bool = False, count: int = 50, offset: int = 0,
                     confirm: bool = False) -> dict:
        """ADMIN. The venues the weekly harvest takes new items from.
        action list: subscribed venues by name with the items held from
        each (contains: a part of the venue's or publisher's name; count,
        offset). add (name, source_id = the venue's OpenAlex source ID;
        optional issns, publisher, category, needs_filter for a journal too
        broad to take whole): subscribe. remove (name): stop harvesting;
        its items stay. add and remove return a preview first and act only
        with confirm=true, after the admin's yes."""
        return self.server.admin(
            "venues", action=action, name=name, contains=contains or None, source_id=source_id,
            issns=issns, publisher=publisher, category=category,
            needs_filter=needs_filter or None, count=count, offset=offset or None,
            confirm=confirm or None)

    def admin_runs(self, action: str = "list", kind: str | None = None, dry_run: bool = False,
                   count: int = 10, confirm: bool = False) -> dict:
        """ADMIN. Harvests (kind surveillance) and backups. action list:
        the next scheduled moment, what is queued or running, and the
        latest runs with their result in one line. start (kind; dry_run
        for a harvest that only counts): queue a run for the worker now.
        start returns a preview first and acts only with confirm=true,
        after the admin's yes. The weekly moment is the settings
        surveillance.day and surveillance.hour."""
        return self.server.admin("runs", action=action, kind=kind, dry_run=dry_run or None,
                                 count=count, confirm=confirm or None)

    def admin_jobs(self, action: str = "list", status: str | None = None,
                   job_id: int | None = None, count: int = 20, confirm: bool = False) -> dict:
        """ADMIN. The worker's jobs, mostly members' uploaded PDFs. action
        list: counts by kind and status, failures grouped by reason, and
        the newest jobs (status: waiting, running, done or failed). show
        (job_id): one job in full. retry (job_id): run a failed job again;
        a preview first, done only with confirm=true after the admin's
        yes. A failure that starts with "Rejected:" is final."""
        return self.server.admin("jobs", action=action, status=status, job_id=job_id,
                                 count=count, confirm=confirm or None)


ADMIN_TOOL_NAMES = ("admin_overview", "admin_settings", "admin_members", "admin_venues",
                    "admin_runs", "admin_jobs")
TOOL_NAMES = ("lookup", "list_items", "search_items", "search_passages", "item", "facets",
              "bibtex", "status", "set_read", "set_mark", "set_export_bibtex",
              "pdf_download", "pdf_upload", "pdf_upload_progress",
              "zotero_add_items", "zotero_pdf_upload",
              "collections", "collection", "collection_add", "collection_remove",
              "collection_edit", "history_record")


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


class Consent:
    """Whether the member has agreed to the notice in this agent session.
    One client process serves one agent session; the agreement lives only
    as long as the process, so every session asks again.

    The question goes to the member through the agent app's own dialog
    (MCP elicitation), so the answer is theirs, not the agent's. On a
    2026-07-28 connection the tool returns an "input required" result and
    the agent app asks, then calls the tool again with the answer; on an
    older connection the client asks directly. Calls arriving while the
    question is open wait for its answer instead of asking again.

    An agent app that cannot show such a dialog is served only when the
    member has accepted the notice in a terminal (`recorded` says so)."""

    KEY = "scriptorium_notice"
    SCHEMA = {"type": "object", "properties": {}}  # no fields: Accept or Decline
    WAIT_SECONDS = 600  # how long a call waits for the member to answer the open question

    def __init__(self, recorded=lambda: False):
        self.agreed = False
        self._open = None  # an asyncio.Event while the question is open
        self._recorded = recorded  # whether the settings hold an acceptance

    def _cannot_ask(self, cause: Exception | None = None) -> None:
        """The agent app has no dialog: the acceptance recorded in a terminal
        counts, and without one the member is told how to give it."""
        if self._recorded():
            self.agreed = True
            return None
        raise ServerError(CANNOT_ASK) from cause

    @staticmethod
    def _app_can_ask(ctx) -> bool:
        try:
            return ctx.session.client_params.capabilities.elicitation is not None
        except AttributeError:  # no capabilities stated: try, and see
            return True

    @staticmethod
    def _yes(answer) -> bool:
        return getattr(answer, "action", None) == "accept"

    def _settle(self, answer) -> None:
        """Record the member's answer and release the calls waiting for it."""
        self.agreed = self._yes(answer)
        if self._open is not None:
            self._open.set()
            self._open = None
        if not self.agreed:
            raise ServerError(NOT_AGREED)

    async def _wait(self) -> None:
        import asyncio

        try:
            await asyncio.wait_for(self._open.wait(), self.WAIT_SECONDS)
        except TimeoutError:
            pass
        if not self.agreed:
            raise ServerError(NOT_AGREED)

    async def check(self):
        """None when the member has agreed (now or earlier in the session); an
        "input required" result for the tool to return when the question must
        be asked; ServerError when they answered anything but "yes", or the
        agent app cannot ask."""
        import asyncio

        import mcp.types as mt
        from fastmcp.server.dependencies import get_context
        from mcp_types.version import MODERN_PROTOCOL_VERSIONS

        if self.agreed:
            return None
        ctx = get_context()
        if not self._app_can_ask(ctx):
            return self._cannot_ask()
        context = ctx.request_context
        modern = context is not None and context.protocol_version in MODERN_PROTOCOL_VERSIONS
        if modern:
            answer = (ctx.input_responses or {}).get(self.KEY)
            if answer is not None:  # the call that asked, sent again with the answer
                return self._settle(answer)
            if self._open is not None:  # another call asked; wait for that answer
                return await self._wait()
            self._open = asyncio.Event()
            return mt.InputRequiredResult(input_requests={self.KEY: mt.ElicitRequest(
                params=mt.ElicitRequestFormParams(message=DIALOG, requested_schema=self.SCHEMA))})
        if self._open is not None:
            return await self._wait()
        self._open = asyncio.Event()
        try:
            # ctx.elicit needs a field; the session's own request takes a form
            # with none, which shows only Accept and Decline
            answer = await ctx.session.elicit(message=DIALOG, requested_schema=self.SCHEMA,
                                              related_request_id=ctx.request_id)
        except Exception as exc:  # noqa: BLE001 — the agent app cannot ask
            self._open.set()
            self._open = None
            return self._cannot_ask(exc)
        return self._settle(answer)


def build_server(tools: Tools | None = None) -> FastMCP:
    """The MCP server with every tool registered, and the admin menu's
    tools as well when the server says the member is an admin. Each call
    first makes sure the member has agreed to the notice in this session,
    and notes which agent is asking, so the server's usage log can record
    it."""
    import functools

    import anyio

    tools = tools or Tools()
    consent = Consent(lambda: member_notice.accepted(client_settings.load()))
    admin = tools.admin_known()
    mcp = FastMCP("scriptorium", instructions=INSTRUCTIONS + (ADMIN_INSTRUCTIONS if admin else ""))
    shown = {"admin": admin, "asked": False}

    def learn() -> None:
        """Once per session, after the notice is accepted: ask the server
        whether the member is an admin. A new admin gets the menu at once
        where the agent app follows a change of the tool list, and
        otherwise at the next start."""
        try:
            if tools.learn_admin() and not shown["admin"]:
                shown["admin"] = True
                for name in ADMIN_TOOL_NAMES:
                    register(name)
        except Exception:  # noqa: BLE001 — the member's own request must still be served
            pass

    def register(name: str) -> None:
        method = getattr(tools, name)

        @functools.wraps(method)
        async def tool(*args, _method=method, **kwargs):
            if (ask := await consent.check()) is not None:
                return ask  # the agent app asks the member, then calls again
            tools.server.agent = agent_name() or tools.server.agent
            if not shown["asked"]:
                shown["asked"] = True
                await anyio.to_thread.run_sync(learn)
            return await anyio.to_thread.run_sync(functools.partial(_method, *args, **kwargs))

        del tool.__wrapped__  # the tool's inputs are the method's, as wraps() copied them
        tool.__signature__ = __import__("inspect").signature(method)
        mcp.add_tool(tool)

    for name in TOOL_NAMES + (ADMIN_TOOL_NAMES if admin else ()):
        register(name)
    return mcp
