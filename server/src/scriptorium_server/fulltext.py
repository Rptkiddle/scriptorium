"""From a PDF to its passages: GROBID parses the PDF (grobid.py), its TEI is
kept (tei.TeiStore), and the TEI is cut into passages (tei.passages).

Reading a PDF with GROBID and cutting the TEI are separate steps, so
passages can be cut again later from the stored TEI without GROBID.

The full-text job (process) takes one donated PDF through these steps,
after checking that the PDF is the publication it is donated for
(mismatch), and stores the stripped PDF (filestore.py).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from xml.etree import ElementTree as ET

import psycopg

from scriptorium_server import items
from scriptorium_server.embedding import Embedder, embed_pending
from scriptorium_server.filestore import FileStore, UnreadablePdf, digest
from scriptorium_server.grobid import Grobid
from scriptorium_server.identity import classify, norm_doi, normalise_title
from scriptorium_server.tei import T, EmptyBody, Passage, TeiStore, _outside, passages

# Raised whenever the passages a TEI yields change (the cutting rules) or
# the GROBID request changes (tei.GROBID_REQUEST). A rules-only change
# re-cuts from the stored TEI; a request change also makes GROBID read the
# PDF again (the stored TEI no longer matches). 4: download stamps removed.
PARSER_VERSION = 4


class ParseError(RuntimeError):
    """Parsing failed for one item; carries the item key."""

    def __init__(self, item_key: str, reason: str):
        self.item_key = item_key
        self.reason = reason
        super().__init__(f"{item_key}: {reason}")


def read_pdf(item_key: str, pdf_path: str | Path, content_hash: str, store: TeiStore,
             grobid: Grobid) -> None:
    """Have GROBID read the PDF and keep the TEI, unless a TEI of this very
    PDF (same content hash, same request) is stored already. Raises
    ParseError when GROBID rejects the PDF; grobid.GrobidUnavailable when
    GROBID cannot be started."""
    if store.usable(item_key, content_hash):
        return
    started = time.monotonic()
    status, body = grobid.parse(pdf_path)
    secs = time.monotonic() - started
    if status != 200:
        message = body.decode(errors="replace").strip()[:300]
        store.put(item_key, str(pdf_path), content_hash, grobid.version(), status, secs,
                  error=message)
        raise ParseError(item_key, f"GROBID could not read the PDF (HTTP {status}): {message}")
    store.put(item_key, str(pdf_path), content_hash, grobid.version(), 200, secs, tei=body)


def parse_pdf(item_key: str, pdf_path: str | Path, content_hash: str, store: TeiStore,
              grobid: Grobid) -> list[Passage]:
    """The PDF's passages, from its stored TEI (GROBID parses the PDF first
    if none matches). Raises ParseError on failure."""
    read_pdf(item_key, pdf_path, content_hash, store, grobid)
    try:
        return passages(store.read(item_key))
    except EmptyBody as exc:
        raise ParseError(item_key, f"{exc} (a scan, or a short item whose text GROBID reads"
                                   " as the abstract)") from exc
    except Exception as exc:  # a damaged TEI file surfaces as varied errors
        raise ParseError(item_key, f"unreadable TEI: {type(exc).__name__}: {exc}") from exc


# --- is this PDF the publication it is said to be? ---------------------------


def header(tei_xml: bytes) -> dict:
    """What GROBID read from the PDF's first page: {"title", "surnames", "doi"}
    (empty when GROBID found none)."""
    root = ET.fromstring(tei_xml)
    head = root.find(f"{T}teiHeader")
    if head is None:
        return {"title": "", "surnames": [], "doi": ""}
    title = head.find(f"{T}fileDesc/{T}titleStmt/{T}title")
    analytic = head.find(f".//{T}sourceDesc/{T}biblStruct/{T}analytic")
    surnames = ["".join(s.itertext()).strip() for s in
                (analytic.iter(f"{T}surname") if analytic is not None else [])]
    doi = next((("".join(i.itertext())).strip() for i in head.iter(f"{T}idno")
                if (i.get("type") or "").upper() == "DOI"), "")
    return {"title": " ".join("".join(title.itertext()).split()) if title is not None else "",
            "surnames": [s for s in surnames if s], "doi": doi}


TITLE_SAME = 0.85  # similarity of two normalised titles counted as the same title
FRONT_TITLE_WORDS = 5  # a title this long or longer, found on the front, names the paper
FRONT_WORDS = 300  # how much of the body counts as the front of the paper


def front(tei_xml: bytes, words: int = FRONT_WORDS) -> str:
    """The front of the paper, normalised like a title: what GROBID put in
    the header (title, authors, abstract) and the first `words` words of
    the body, without footnotes or the reference list, so a publication the
    paper merely cites is not taken for the paper itself."""
    root = ET.fromstring(tei_xml)
    head = root.find(f"{T}teiHeader")
    parts = [" ".join(head.itertext())] if head is not None else []
    body = root.find(f"{T}text/{T}body")
    count = 0
    if body is not None:
        for p in _outside(body, f"{T}figure", f"{T}note"):
            if count >= words:
                break
            text = " ".join(" ".join(p.itertext()).split()[:words - count])
            parts.append(text)
            count += len(text.split())
    return normalise_title(" ".join(parts))


def accepted_by(tei_xml: bytes, title: str, creators: list[dict], doi: str = "") -> str | None:
    """Which rule finds the parsed PDF to be the publication with this
    title and DOI; None when none does. The rules, in order:
    - "doi": the DOI GROBID read from the PDF's first page is the item's;
    - "same title": the title GROBID read is the item's (similarity >=
      TITLE_SAME, or one begins with the other, as when a subtitle is missing);
    - "title on front": the item's title, of FRONT_TITLE_WORDS words or
      more, is on the front of the paper (front()), for PDFs whose first
      page GROBID could not read as a title page (it often takes the
      journal's name for the title).
    Shared authors are not enough: a later paper by the same authors on the
    same subject is a different publication. `creators` is kept for the
    caller's convenience and for a rule that may need it."""
    wanted = normalise_title(title)
    found = header(tei_xml)
    if doi and found["doi"] and norm_doi(found["doi"]) == norm_doi(doi):
        return "doi"
    got = normalise_title(found["title"])
    if wanted and got:
        ratio = SequenceMatcher(None, wanted, got, autojunk=False).ratio()
        short, long = sorted((wanted, got), key=len)
        if ratio >= TITLE_SAME or (len(short.split()) >= 3 and long.startswith(short)):
            return "same title"
    if len(wanted.split()) >= FRONT_TITLE_WORDS and f" {wanted} " in f" {front(tei_xml)} ":
        return "title on front"
    return None


def mismatch(tei_xml: bytes, title: str, creators: list[dict], doi: str = "") -> str | None:
    """None when the parsed PDF is the publication with this title, creators
    and DOI (accepted_by); otherwise the reason it is not."""
    if accepted_by(tei_xml, title, creators, doi):
        return None
    if not header(tei_xml)["title"]:
        return (f"the PDF's title could not be read, and the item's title ({title!r}) is not"
                " on its first page")
    return f"the PDF's title ({header(tei_xml)['title']!r}) is not the item's ({title!r})"


# --- the full-text job: one donated PDF ----------------------------------------


class Rejected(ValueError):
    """A donated PDF that is not kept, with the reason the member sees."""


@dataclass
class Context:
    """What the full-text job works with."""
    grobid: Grobid
    tei: TeiStore
    files: FileStore
    embedder: Embedder | None = None
    registry: Callable | None = None  # sources.crossref.fetch: (kind, value) -> (record, relation) | None
    batch_size: int = 32


def process(conn: psycopg.Connection, job, ctx: Context) -> str:
    """The job for one donated PDF (jobs of kind "fulltext"). The payload
    holds "file" (the PDF in the holding area) and the publication it is
    for: "item" (an item key) or "identifier" (a DOI or arXiv ID); with
    neither, the DOI GROBID reads from the PDF's first page is used.

    The item is found, or, for an identifier the collection does not hold,
    fetched from the registry and admitted (provenance "donation"). An item
    with a PDF already keeps it, and the donation is let go. The PDF the
    seed import awaits for an item is stored without parsing. Otherwise
    GROBID parses the PDF; the PDF must be the item's publication
    (mismatch()); its passages replace the item's, the stripped PDF is
    stored, and the passages get their vectors.

    A PDF that is not kept for good reason (not the publication, not
    readable, no identifier) raises Rejected and its file is deleted. When
    GROBID or the registry cannot be reached, the error is raised and the
    file stays, so the job can be run again (`scriptorium jobs retry`).
    Safe to run twice: once the file is gone, there is nothing to do."""
    path = Path(job.payload["file"])
    if not path.exists():
        return "nothing to do: this file was processed or let go already"
    try:
        summary = _process(conn, job, ctx, path)
    except (Rejected, ParseError, UnreadablePdf) as exc:
        conn.rollback()
        path.unlink(missing_ok=True)
        reason = exc.reason if isinstance(exc, ParseError) else str(exc)
        raise Rejected(f"not kept: {reason}") from exc
    path.unlink(missing_ok=True)
    return summary


def _resolve(conn: psycopg.Connection, identifier: str, member: int | None,
             registry: Callable | None) -> str:
    """The key of the item the identifier names, admitting it from the
    registry when the collection does not hold it."""
    kind, value = classify(identifier)
    if kind not in ("doi", "arxiv"):
        raise Rejected(f"{identifier!r} is not a DOI or arXiv ID")
    if key := items.holder(conn, [f"{kind}:{value}"]):
        return key
    if registry is None:
        raise Rejected(f"{identifier} is not in the collection")
    known = registry(kind, value)  # RegistryUnavailable: the job can run again
    if known is None:
        raise Rejected(f"the registry does not know {identifier}")
    record, relation = known
    key, _ = items.admit(conn, record, "donation", member, relation=relation)
    conn.commit()  # the publication is admitted whatever becomes of the PDF
    return key


def _parse(ctx: Context, key: str, path: Path, md5: str) -> bytes:
    """The PDF's TEI: the stored one when it is of this very file, else
    GROBID's. Nothing is stored here: a TEI is kept only for a PDF that is
    kept (store_passages)."""
    if ctx.tei.usable(key, md5):
        return ctx.tei.read(key)
    status, body = ctx.grobid.parse(path)
    if status != 200:
        raise Rejected(f"GROBID could not read the PDF (HTTP {status}):"
                       f" {body.decode(errors='replace').strip()[:300]}")
    return body


def _store_file(conn: psycopg.Connection, ctx: Context, path: Path, key: str, md5: str,
                member: int | None) -> None:
    sha256, size = ctx.files.put(path)
    conn.execute("INSERT INTO files (sha256, item, bytes, donated_by, source_md5)"
                 " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                 (sha256, key, size, member, md5))
    conn.execute("DELETE FROM pending_files WHERE item = %s", (key,))


def _process(conn: psycopg.Connection, job, ctx: Context, path: Path) -> str:
    md5 = digest(path, "md5")
    tei_xml = None
    if key := job.payload.get("item"):
        if items.get(conn, key) is None:
            raise Rejected(f"there is no item {key} in the collection")
    elif identifier := job.payload.get("identifier"):
        key = _resolve(conn, identifier, job.member, ctx.registry)
    else:
        status, tei_xml = ctx.grobid.parse(path)
        if status != 200:
            raise Rejected(f"GROBID could not read the PDF (HTTP {status})")
        if not (doi := header(tei_xml)["doi"]):
            raise Rejected("no DOI or arXiv ID was given, and none is on the PDF's first page")
        key = _resolve(conn, doi, job.member, ctx.registry)
    if conn.execute("SELECT 1 FROM files WHERE item = %s", (key,)).fetchone():
        return f"{key} has a PDF already; this one was let go"
    if conn.execute("SELECT 1 FROM pending_files WHERE item = %s AND md5 = %s",
                    (key, md5)).fetchone():
        _store_file(conn, ctx, path, key, md5, job.member)
        conn.commit()
        return f"{key}: the PDF its full text was made from, stored"
    tei_xml = tei_xml or _parse(ctx, key, path, md5)
    item = items.get(conn, key)
    if reason := mismatch(tei_xml, item["title"], item["creators"] or [], item["doi"] or ""):
        raise Rejected(reason)
    try:
        found = passages(tei_xml)
    except EmptyBody as exc:
        raise Rejected(f"{exc} (a scan, or a short item whose text GROBID reads as the"
                       " abstract)") from exc
    _store_file(conn, ctx, path, key, md5, job.member)
    conn.execute("DELETE FROM passages WHERE item = %s", (key,))
    with conn.cursor().copy("COPY passages (item, idx, part, pages, text, embed_text, artefact)"
                            " FROM STDIN") as copy:
        for idx, p in enumerate(found):
            copy.write_row((key, idx, p.part, p.pages, p.text,
                            p.embed_text if p.embed_text != p.text else None, p.artefact))
    conn.execute("UPDATE items SET has_fulltext = true WHERE key = %s", (key,))
    ctx.tei.put(key, path.name, md5, ctx.grobid.version(), 200, 0.0, tei=tei_xml)
    conn.commit()
    if ctx.embedder is not None:  # the passages, and the item when the donation admitted it
        embed_pending(conn, ctx.embedder, ctx.batch_size)
    return f"{key}: {len(found)} passages from the donated PDF"
