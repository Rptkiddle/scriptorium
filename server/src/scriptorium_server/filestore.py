"""The file store: PDFs held for the collection, each stored once by the
SHA-256 of its stored bytes, under <data folder>/pdfs/<first two>/<sha256>.pdf.

A PDF is stored stripped: whatever a reader added to it, or what it says
about whoever made or handled the file, is removed before it is kept.
Removed:
- annotations, except links (highlights, notes, ink, filled-in form fields);
- the PDF's form;
- the document information (author, creating software, title, dates) and
  the XMP metadata;
- files embedded in the PDF.
Kept: the pages as printed, links and bookmarks. Text printed on the pages
(a publisher's download stamp, for instance) cannot be removed reliably
and stays. Encryption that allows opening without a password is removed.

Stripping the same file twice gives the same bytes (the PDF's /ID is made
from its content, not the time), so a file donated twice is stored once.
The MD5 of the file as it arrived is kept beside it in the table `files`.
"""

import hashlib
import os
from pathlib import Path

import pikepdf


class UnreadablePdf(ValueError):
    """The file is not a PDF that can be read (damaged, or locked by a password)."""


def digest(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def strip_pdf(source: Path, target: Path) -> None:
    """Write `source` to `target` without annotations (links kept), form,
    document information, XMP metadata and embedded files."""
    try:
        with pikepdf.open(source) as pdf:
            for page in pdf.pages:
                annots = page.obj.get("/Annots")
                if annots is not None:
                    links = [a for a in annots if a.get("/Subtype") == pikepdf.Name.Link]
                    if links:
                        page.obj.Annots = pikepdf.Array(links)
                    else:
                        del page.obj["/Annots"]
                if "/PieceInfo" in page.obj:  # private data an editing program left
                    del page.obj["/PieceInfo"]
            root = pdf.Root
            for key in ("/AcroForm", "/Metadata", "/AF", "/PieceInfo"):
                if key in root:
                    del root[key]
            names = root.get("/Names")
            if names is not None and "/EmbeddedFiles" in names:
                del names["/EmbeddedFiles"]
            if "/Info" in pdf.trailer:
                del pdf.trailer["/Info"]
            pdf.save(target, deterministic_id=True)
    except Exception as exc:  # noqa: BLE001 — whatever one odd file raises, it is that file's
        raise UnreadablePdf(f"{type(exc).__name__}: {exc}") from exc


class FileStore:
    """PDFs by content hash under <root>/pdfs/."""

    def __init__(self, root: Path):
        self.dir = Path(root) / "pdfs"

    def path(self, sha256: str) -> Path:
        return self.dir / sha256[:2] / f"{sha256}.pdf"

    def put(self, source: Path) -> tuple[str, int]:
        """Store a stripped copy of `source`; returns (SHA-256, size) of the
        stored file. The source is left as it is. UnreadablePdf when it
        cannot be read as a PDF."""
        self.dir.mkdir(parents=True, exist_ok=True)
        part = self.dir / f".{os.getpid()}-{Path(source).name}.part"
        try:
            strip_pdf(Path(source), part)
            sha256 = digest(part, "sha256")
            size = part.stat().st_size
            final = self.path(sha256)
            if final.exists():
                part.unlink()
            else:
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(part, final)
            return sha256, size
        finally:
            part.unlink(missing_ok=True)
