"""From a PDF to its passages: GROBID parses the PDF (grobid.py), its TEI is
kept (tei.TeiStore), and the TEI is cut into passages (tei.passages).

Reading a PDF with GROBID and cutting the TEI are separate steps, so
passages can be cut again later from the stored TEI without GROBID.
"""

import time
from pathlib import Path

from scriptorium_server.grobid import Grobid
from scriptorium_server.tei import EmptyBody, Passage, TeiStore, passages

# Raised whenever the passages a TEI yields change (the cutting rules) or
# the GROBID request changes (tei.GROBID_REQUEST). A rules-only change
# re-cuts from the stored TEI; a request change also makes GROBID read the
# PDF again (the stored TEI no longer matches).
PARSER_VERSION = 3


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
