"""From a paper's PDF to its passages: GROBID reads the PDF (grobid.py), its
TEI is kept (tei.TeiStore), and the TEI is cut into passages (tei.passages).

Reading a PDF with GROBID and cutting the TEI are separate steps, so a
sync can read every new PDF first (GROBID running) and embed afterwards,
and passages can be cut again later from the stored TEI without GROBID.
"""

import json
import os
import time
from pathlib import Path

from paperqa.utils import md5sum

from scriptorium.config import Config
from scriptorium.grobid import Grobid
from scriptorium.tei import EmptyBody, Passage, TeiStore, passages
from scriptorium.zotero import PaperRecord

# Bumped whenever the passages a TEI yields change (the cutting rules) or
# the GROBID request changes (tei.GROBID_REQUEST); a mismatch with the
# version stamped in the index marks every older paper for re-indexing.
# A rules-only change re-cuts from the stored TEI; a request change also
# makes GROBID read the PDF again (the stored TEI no longer matches).
PARSER_VERSION = 3


class ParseError(RuntimeError):
    """Parsing failed for one paper; carries the Zotero item key."""

    def __init__(self, item_key: str, reason: str):
        self.item_key = item_key
        self.reason = reason
        super().__init__(f"{item_key}: {reason}")


class PdfHashes:
    """Content hashes (MD5) of PDFs, cached by file size and modification
    time so a sync reads only the files that changed.

    The cache is disposable and lives in its own file beside the index:
    losing it costs one full re-hash, nothing more. A file replaced by
    another of identical size and modification time keeps its old hash.
    """

    def __init__(self, path: Path):
        self.path = path
        try:
            self._cache: dict[str, list] = json.loads(path.read_text())
        except (OSError, ValueError):
            self._cache = {}
        self._seen: set[str] = set()

    def of(self, record: PaperRecord) -> str:
        """The record's PDF hash. OSError (file unreadable) reaches the caller."""
        path = record.pdf_path
        st = os.stat(path)
        self._seen.add(path)
        hit = self._cache.get(path)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return hit[2]
        digest = md5sum(path)
        self._cache[path] = [st.st_size, st.st_mtime_ns, digest]
        return digest

    def save(self, prune: bool = False) -> None:
        """Write the cache. prune drops paths not hashed since loading; pass
        it only after a complete pass over the library."""
        if prune:
            self._cache = {p: v for p, v in self._cache.items() if p in self._seen}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._cache))
        os.replace(tmp, self.path)


def tei_store(config: Config) -> TeiStore:
    return TeiStore(config.index_dir / "tei")


def grobid_from_settings(config: Config) -> Grobid:
    from scriptorium.settings import load_settings

    general = load_settings(config)["general"]
    return Grobid(general["grobid_dir"], port=general["grobid_port"],
                  java=general["grobid_java_home"], log_path=config.index_dir / "grobid.log")


def read_pdf(record: PaperRecord, content_hash: str, store: TeiStore, grobid: Grobid) -> None:
    """Have GROBID read the record's PDF and keep the TEI, unless a TEI of
    this very PDF (same md5, same request) is stored already. Raises
    ParseError when GROBID rejects the PDF; grobid.GrobidUnavailable when
    GROBID cannot be started."""
    if record.pdf_status != "ok":
        raise ParseError(record.item_key, f"no usable PDF (status: {record.pdf_status})")
    if store.usable(record.item_key, content_hash):
        return
    started = time.monotonic()
    status, body = grobid.parse(record.pdf_path)
    secs = time.monotonic() - started
    if status != 200:
        message = body.decode(errors="replace").strip()[:300]
        store.put(record.item_key, record.pdf_path, content_hash, grobid.version(),
                  status, secs, error=message)
        raise ParseError(record.item_key, f"GROBID could not read the PDF (HTTP {status}):"
                                          f" {message}")
    store.put(record.item_key, record.pdf_path, content_hash, grobid.version(), 200, secs,
              tei=body)


def parse_paper(record: PaperRecord, content_hash: str, store: TeiStore,
                grobid: Grobid) -> list[Passage]:
    """The record's passages, from its stored TEI (GROBID reads the PDF
    first if none matches). Raises ParseError on failure."""
    read_pdf(record, content_hash, store, grobid)
    try:
        return passages(store.read(record.item_key))
    except EmptyBody as exc:
        raise ParseError(record.item_key, f"{exc} (a scan without a text layer?)") from exc
    except Exception as exc:  # a damaged TEI file surfaces as varied errors
        raise ParseError(record.item_key, f"unreadable TEI: {type(exc).__name__}: {exc}") from exc
