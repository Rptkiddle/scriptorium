"""Parse and chunk paper PDFs using paperqa's reader machinery.

Input: a PaperRecord (Zotero metadata ground truth) with a PDF on disk.
Output: paperqa Text chunks whose Doc carries the Zotero metadata. Text-only
parsing; no media extraction and no LLM involvement anywhere.
"""

import asyncio
import json
import os
from pathlib import Path

from paperqa.readers import read_doc
from paperqa.types import Doc, Text
from paperqa.utils import ImpossibleParsingError, md5sum
from paperqa_pymupdf import parse_pdf_to_pages

from scriptorium.biblio import apply_span, detect_bibliography
from scriptorium.config import Config
from scriptorium.zotero import PaperRecord

# Bumped whenever parsing behaviour changes; a mismatch with the version
# stamped in the sync state marks every older item as needing re-ingest.
PARSER_VERSION = 2


def parse_pdf_trimmed(path, **kwargs):
    """PaperQA2's PyMuPDF parser, minus the trailing bibliography.

    The single scriptorium deviation from PaperQA2 parsing: after extraction,
    the references section located by scriptorium.biblio is removed from the
    per-page text before chunking. Detection failure means no trimming.
    """
    parsed = parse_pdf_to_pages(path, **kwargs)
    try:
        span = detect_bibliography(str(path))
    except Exception:  # noqa: BLE001 — detection failure means no trimming
        span = None
    if span is not None:
        trimmed = apply_span(parsed.content, span)
        if trimmed:  # never trim away the whole document
            parsed.content = trimmed
            parsed.metadata.total_parsed_text_length = sum(
                len(t) for t in trimmed.values()
            )
    return parsed


class ParseError(RuntimeError):
    """Parsing failed for one paper; carries the Zotero item key."""

    def __init__(self, item_key: str, reason: str):
        self.item_key = item_key
        self.reason = reason
        super().__init__(f"{item_key}: {reason}")


class PdfHashes:
    """Content hashes (MD5) of PDFs, cached by file size and modification
    time so a sync reads only the files that changed.

    The cache is disposable and lives in its own file beside state.json:
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


def record_to_doc(record: PaperRecord, content_hash: str | None = None) -> Doc:
    """Build the paperqa Doc for a record (citation key = docname)."""
    return Doc(
        docname=record.citation_key or record.item_key,
        citation=record.citation,
        dockey=record.item_key,
        content_hash=content_hash or md5sum(record.pdf_path),
    )


def parse_paper(
    record: PaperRecord, config: Config, content_hash: str | None = None,
) -> list[Text]:
    """Parse the record's PDF into chunks. Raises ParseError on failure.
    Pass content_hash when the caller has it, to avoid reading the file twice."""
    if record.pdf_status != "ok":
        raise ParseError(record.item_key, f"no usable PDF (status: {record.pdf_status})")
    doc = record_to_doc(record, content_hash)
    try:
        chunks = asyncio.run(
            read_doc(
                record.pdf_path,
                doc,
                chunk_chars=config.chunk_chars,
                overlap=config.chunk_overlap,
                parse_pdf=parse_pdf_trimmed,
                parse_media=False,
            )
        )
    except ImpossibleParsingError as exc:
        raise ParseError(record.item_key, f"unparseable PDF: {exc}") from exc
    except Exception as exc:  # corrupt/encrypted files surface as varied errors
        raise ParseError(record.item_key, f"{type(exc).__name__}: {exc}") from exc
    if not chunks or not any(c.text.strip() for c in chunks):
        raise ParseError(record.item_key, "PDF parsed to empty text (image-only scan?)")
    return chunks
