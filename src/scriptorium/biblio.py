"""Bibliography-span detection for parse-time exclusion.

Locates the trailing references section of an academic PDF so parsing can skip
it (a scriptorium deviation from PaperQA2, which parses and embeds full
documents). Detection is a cascade, ordered cheap-and-precise to fallback:

1. Outline (``doc.get_toc()``): an entry matching the heading pattern gives
   the start page; the next entry at the same or higher level gives the page
   to resume at (e.g. an appendix). Exact when present.
2. Typography: scan pages back to front for a standalone heading line that is
   bold or larger than the modal body font size. Back-scanning skips early
   false positives (front-matter mentions, per-chapter bibliographies). If a
   named post-bibliography heading (appendix, acknowledgements, ...) appears
   later, resume there; otherwise the section runs to the end.
3. Reference density: for documents with neither outline nor detectable
   heading, drop the longest run of citation-dense pages (year/DOI patterns
   per line) anchored at the end of the document.

If nothing is detected the document is parsed in full — every failure mode
keeps text rather than losing it. Known limitations: books with per-chapter
bibliographies keep all but the last; appendices headed by unnamed markers
(e.g. arXiv-style "A.") are dropped along with the references; only English
headings are recognised.
"""

import collections
import re
from dataclasses import dataclass

import pymupdf

HEADING = re.compile(
    r"^\s*(?:\d+\.?\s*)?(references?|bibliography|works cited|literature cited|reference list)\s*:?\s*$",
    re.IGNORECASE,
)
END_HEADING = re.compile(
    r"^\s*(?:\d+\.?\s*)?(appendix|appendices|supplementar\w*|supporting information"
    r"|online appendix|acknowledg\w*|about the authors?|author biograph\w*"
    r"|endnotes|tables|figures|glossary|index)\b.{0,40}$",
    re.IGNORECASE,
)
YEAR = re.compile(r"\((?:19|20)\d{2}[a-z]?\)|,\s*(?:19|20)\d{2}[a-z]?[.;]")
DOI = re.compile(r"doi\.org|doi:|https?://|arxiv", re.IGNORECASE)

DENSITY_THRESHOLD = 0.35  # (year+doi matches) per non-empty line
BOLD_FLAG = 16


@dataclass(frozen=True)
class BiblioSpan:
    """A detected references section, in 1-indexed pages.

    ``cut_at_heading``: True when the start page holds a regex-matching
    heading line and text above it should be kept; False when the whole start
    page is part of the section (density tier). ``resume_page`` is the first
    page AFTER the section (None: it runs to the end of the document).
    """

    start_page: int
    cut_at_heading: bool
    resume_page: int | None
    tier: str


def detect_bibliography(pdf_path: str) -> BiblioSpan | None:
    """Run the detection cascade. Returns None if nothing is detected."""
    with pymupdf.open(pdf_path) as doc:
        return _toc_tier(doc) or _typo_tier(doc) or _density_tier(doc)


def _toc_tier(doc) -> BiblioSpan | None:
    # Entries with page < 1 have unresolved destinations; using one would
    # cut from, or resume at, a meaningless page.
    toc = [(lv, t, p) for lv, t, p in doc.get_toc() if p >= 1]
    for idx, (level, title, page) in enumerate(toc):
        if HEADING.match(title.strip()):
            resume = next(
                (p for lv, _t, p in toc[idx + 1 :] if lv <= level and p >= page), None
            )
            return BiblioSpan(page, cut_at_heading=True, resume_page=resume, tier="toc")
    return None


def _body_font_size(doc) -> float:
    sizes: collections.Counter = collections.Counter()
    step = max(1, doc.page_count // 10)
    for i in range(0, doc.page_count, step):
        for block in doc.load_page(i).get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip():
                        sizes[round(span["size"], 1)] += len(span["text"])
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _typo_tier(doc) -> BiblioSpan | None:
    body = _body_font_size(doc)
    for pno in range(doc.page_count - 1, -1, -1):
        for block in doc.load_page(pno).get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s["text"] for s in spans).strip()
                if not HEADING.match(text):
                    continue
                size = max(s["size"] for s in spans)
                if size > body + 0.5 or any(s["flags"] & BOLD_FLAG for s in spans):
                    resume = _named_end_page(doc, pno + 1)
                    return BiblioSpan(
                        pno + 1, cut_at_heading=True, resume_page=resume, tier="typo"
                    )
    return None


def _named_end_page(doc, after_page: int) -> int | None:
    """First page after ``after_page`` opening a named post-bibliography section."""
    for pno in range(after_page, doc.page_count):
        for raw in doc.load_page(pno).get_text("text").splitlines():
            if END_HEADING.match(raw.strip()):
                return pno + 1
    return None


def _density_tier(doc) -> BiblioSpan | None:
    dense = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text("text")
        lines = max(len([ln for ln in text.splitlines() if ln.strip()]), 1)
        score = (len(YEAR.findall(text)) + len(DOI.findall(text))) / lines
        dense.append(score > DENSITY_THRESHOLD)
    best = None
    i, n = 0, len(dense)
    while i < n:
        if dense[i]:
            j = i
            while j + 1 < n and dense[j + 1]:
                j += 1
            if j >= n - 2 and (best is None or j - i > best[1] - best[0]):
                best = (i, j)
            i = j + 1
        else:
            i += 1
    if best and best[1] - best[0] >= 1:  # at least two dense pages
        return BiblioSpan(best[0] + 1, cut_at_heading=False, resume_page=None, tier="density")
    return None


def apply_span(content: dict, span: BiblioSpan) -> dict:
    """Remove the detected section from a per-page text dict (str keys/values)."""
    out: dict = {}
    for key, text in content.items():
        page = int(key)
        if page < span.start_page:
            out[key] = text
        elif page == span.start_page and span.cut_at_heading:
            lines = text.splitlines()
            cut = next((i for i, ln in enumerate(lines) if HEADING.match(ln.strip())), None)
            kept = "\n".join(lines[:cut]) if cut is not None else ""
            if kept.strip():
                out[key] = kept
        elif span.resume_page is not None and page >= span.resume_page:
            out[key] = text
    return out
