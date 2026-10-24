"""GROBID's TEI output: turning it into passages, and keeping it on disk.

GROBID reads a PDF into TEI XML: the body as headed sections of paragraphs
(<p>, each split into sentences <s>), footnotes as <note place="foot">, and
the back matter (references, acknowledgements, funding, appendices) in
<back>. Each sentence carries its position on the page (coords), so every
passage knows its pages.

Passages are taken from three parts of the paper, in this order: body
paragraphs, footnotes, appendix paragraphs. Everything else stays out:
the abstract (item search covers it), figure and table captions and
contents, formulas, references, acknowledgements, funding and data
statements. Excluded text is not lost: the paper's whole TEI is kept
(TeiStore), so passages can be cut again later without GROBID.

Size bounds, per part and in reading order: a block under MIN_WORDS words
is joined to the front of the next block of that part (a fragment such as
"where" left around an equation, a heading read as a paragraph); a short
block left at the end of a part joins the passage before it; a block over
MAX_WORDS is cut between sentences into the fewest, roughly equal pieces.
Every word of the three parts ends up in exactly one passage. A single
"sentence" over MAX_WORDS (a table or number column GROBID read without
sentence breaks) is cut at word boundaries the same way.

Artefacts: a passage whose share of common function words ("the", "of",
"and", and their German, French, Spanish, Italian and Dutch equivalents)
is below PROSE_MIN is marked `artefact`: a table, a matrix, axis labels,
code or a formula rather than prose. It is stored and shown like any
passage but gets no vector, so passage search never returns it. Prose
scores about 0.35; tables and the like score near 0.

A passage has two texts: `text`, stored and shown, the paper's words as
GROBID read them; and `embed_text`, the same without citation and
footnote markers, from which its vector is made (names and years say
nothing about what a paragraph means).
"""

import gzip
import json
import math
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

T = "{http://www.tei-c.org/ns/1.0}"
MIN_WORDS = 20
MAX_WORDS = 300
PROSE_MIN = 0.10

FUNCTION_WORDS = frozenset("""
the of and to in a is that for on with as by are be this we it from or at an
which was not their they these have has more can than its our were been
between also such how other but one into there when both may
der die das und ist zu den mit von im nicht ein eine sich auf für dass es
auch wie
le la les et des est un une du que dans pour pas qui sur au par ce
el los las y en que un una por con para es del se lo
il di che non per una sono della gli nel alla anche
de het een en van is dat niet op zijn met voor ook
""".split())  # noqa: SIM905 — one language per line reads better
_STRIP = ".,;:()[]\"'«»“”‘’"

# GROBID's request for every PDF: page positions on paragraphs and
# sentences, sentence marks, no outside lookups (consolidation off). This
# exact string is recorded beside each stored TEI; a stored TEI made with
# other settings is not used. Changing the settings means changing this
# string AND raising parsing.PARSER_VERSION, so every paper is parsed again.
GROBID_FORM = (("consolidateHeader", "0"), ("consolidateCitations", "0"),
               ("consolidateFunders", "0"), ("segmentSentences", "1"),
               ("teiCoordinates", "p"), ("teiCoordinates", "s"))
GROBID_REQUEST = "processFulltextDocument;" + ";".join(f"{k}={v}" for k, v in GROBID_FORM)

# GROBID sometimes writes its own placeholder ("foot_0") where a footnote
# marker should be; it is not the paper's text.
_FOOT_PLACEHOLDER = re.compile(r"foot_\d+")


class EmptyBody(ValueError):
    """The TEI holds no body text (e.g. a scan GROBID could not read)."""


@dataclass
class Passage:
    text: str        # the paper's words, as stored and shown
    embed_text: str  # text without citation and footnote markers
    pages: str       # "3" or "3-5"; "" when GROBID gave no positions
    part: str        # "body" | "footnote" | "appendix"
    artefact: bool = False  # not prose (PROSE_MIN): stored, not embedded


@dataclass
class _Sentence:
    text: str
    embed: str
    pages: set[int]

    @property
    def words(self) -> int:
        return len(self.text.split())


def _pages(el: ET.Element) -> set[int]:
    """The pages an element's coords cover ("page,x,y,w,h;page,...")."""
    return {int(box.split(",", 1)[0]) for box in el.get("coords", "").split(";")
            if box and box.split(",", 1)[0].isdigit()}


def _texts(el: ET.Element) -> tuple[str, str]:
    """An element's (stored, embed) text. Stored keeps everything but
    GROBID's footnote placeholders; embed also drops citation (bibr) and
    footnote markers. A dropped element's tail text is kept."""
    stored, embed = [el.text or ""], [el.text or ""]
    for child in el:
        is_ref = child.tag == f"{T}ref"
        kind = child.get("type") if is_ref else None
        inner_stored, inner_embed = _texts(child)
        if not (kind == "foot" and _FOOT_PLACEHOLDER.fullmatch(inner_stored.strip())):
            stored.append(inner_stored)
        if kind not in ("bibr", "foot"):
            embed.append(inner_embed)
        stored.append(child.tail or "")
        embed.append(child.tail or "")
    return "".join(stored), "".join(embed)


def _clean(s: str) -> str:
    return " ".join(s.split())


def _sentences(block: ET.Element) -> list[_Sentence]:
    """A block's sentences, each with its pages. Text outside any <s>
    (rare) becomes a sentence of its own with the block's pages."""
    found = list(block.iter(f"{T}s"))
    if not found:
        stored, embed = _texts(block)
        return [_Sentence(_clean(stored), _clean(embed), _pages(block))] if _clean(stored) else []
    out = []
    for s in found:
        stored, embed = _texts(s)
        if _clean(stored):
            out.append(_Sentence(_clean(stored), _clean(embed), _pages(s) or _pages(block)))
    return out


def _outside(root: ET.Element, *tags: str) -> Iterator[ET.Element]:
    """The <p> elements under root that are not inside any of `tags`."""
    def walk(el: ET.Element) -> Iterator[ET.Element]:
        for child in el:
            if child.tag in tags:
                continue
            if child.tag == f"{T}p":
                yield child
            else:
                yield from walk(child)
    yield from walk(root)


def blocks(tei: ET.Element) -> list[tuple[str, list[_Sentence]]]:
    """(part, sentences) for every block the passages are made from, in
    reading order: body paragraphs, footnotes, appendix paragraphs."""
    text = tei.find(f"{T}text")
    body = text.find(f"{T}body") if text is not None else None
    back = text.find(f"{T}back") if text is not None else None
    found: list[tuple[str, list[_Sentence]]] = []
    if body is not None:
        found += [("body", _sentences(p)) for p in _outside(body, f"{T}figure", f"{T}note")]
    if text is not None:
        found += [("footnote", _sentences(n)) for n in text.iter(f"{T}note")
                  if n.get("place") == "foot"]
    if back is not None:
        for div in back.findall(f"{T}div"):
            if div.get("type") == "annex":
                found += [("appendix", _sentences(p))
                          for p in _outside(div, f"{T}figure", f"{T}note")]
    return [(part, ss) for part, ss in found if ss]


def prose_share(text: str) -> float:
    """The share of a text's words that are common function words."""
    words = text.split()
    return sum(w.lower().strip(_STRIP) in FUNCTION_WORDS for w in words) / len(words) if words else 0.0


def _word_pieces(s: _Sentence) -> list[_Sentence]:
    """A sentence over MAX_WORDS cut at word boundaries into the fewest
    roughly equal pieces; the embed text is cut into as many pieces."""
    if s.words <= MAX_WORDS:
        return [s]
    n = math.ceil(s.words / MAX_WORDS)

    def cut(text: str) -> list[str]:
        words = text.split()
        size = math.ceil(len(words) / n)
        return [" ".join(words[i:i + size]) for i in range(0, size * n, size)]

    return [_Sentence(t, e, set(s.pages)) for t, e in zip(cut(s.text), cut(s.embed), strict=True)]


def _split(ss: list[_Sentence]) -> list[list[_Sentence]]:
    """Cut a run of sentences into the fewest pieces of at most MAX_WORDS
    words, cutting between sentences and keeping the pieces about equal
    (a sentence over MAX_WORDS is first cut at word boundaries)."""
    ss = [piece for s in ss for piece in _word_pieces(s)]
    total = sum(s.words for s in ss)
    n = math.ceil(total / MAX_WORDS)
    target = total / n
    pieces: list[list[_Sentence]] = []
    cur: list[_Sentence] = []
    for s in ss:
        size = sum(x.words for x in cur)
        if cur and (size + s.words > MAX_WORDS or (size >= target and len(pieces) < n - 1)):
            pieces.append(cur)
            cur = []
        cur.append(s)
    return [*pieces, cur]


def _passage(ss: list[_Sentence], part: str) -> Passage:
    pages = set().union(*(s.pages for s in ss))
    span = "" if not pages else (str(min(pages)) if min(pages) == max(pages)
                                 else f"{min(pages)}-{max(pages)}")
    text = " ".join(s.text for s in ss)
    return Passage(text=text, embed_text=_clean(" ".join(s.embed for s in ss)), pages=span,
                   part=part, artefact=prose_share(text) < PROSE_MIN)


def passages(tei_xml: bytes) -> list[Passage]:
    """The paper's passages, by the rules in this module's docstring.
    Raises EmptyBody when the TEI holds no body text."""
    found = blocks(ET.fromstring(tei_xml))
    if not any(part == "body" for part, _ in found):
        raise EmptyBody("GROBID found no body text")
    out: list[Passage] = []
    for part in ("body", "footnote", "appendix"):
        runs: list[list[_Sentence]] = []
        pending: list[_Sentence] = []
        for p, ss in found:
            if p != part:
                continue
            ss = pending + ss
            if sum(s.words for s in ss) < MIN_WORDS:
                pending = ss
                continue
            pending = []
            runs += _split(ss) if sum(s.words for s in ss) > MAX_WORDS else [ss]
        if pending:  # a short block left at the end joins the passage before it
            if runs:
                runs[-1] = runs[-1] + pending
            else:
                runs.append(pending)
        out += [_passage(r, part) for r in runs]
    return out


class TeiStore:
    """Each paper's GROBID output, gzipped, as <dir>/<item key>.tei.xml.gz,
    with a record of every GROBID attempt in <dir>/parsed.jsonl (item key,
    source PDF, the PDF's md5, GROBID version, request settings, HTTP
    status, seconds, time). A stored TEI is used only when the latest
    record for its paper shows success with the current settings and the
    md5 of the PDF now attached."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.record = directory / "parsed.jsonl"
        self._latest: dict[str, dict] | None = None

    def path(self, key: str) -> Path:
        return self.dir / f"{key}.tei.xml.gz"

    def latest(self) -> dict[str, dict]:
        if self._latest is None:
            self._latest = {}
            if self.record.exists():
                for line in self.record.read_text().splitlines():
                    if line.strip():
                        row = json.loads(line)
                        self._latest[row["key"]] = row
        return self._latest

    def usable(self, key: str, md5: str) -> bool:
        row = self.latest().get(key)
        return bool(row and row.get("http") == 200 and row.get("md5") == md5
                    and row.get("settings") == GROBID_REQUEST and self.path(key).exists())

    def read(self, key: str) -> bytes:
        return gzip.decompress(self.path(key).read_bytes())

    def _append(self, row: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.record, "a") as f:
            f.write(json.dumps(row) + "\n")
        self.latest()[row["key"]] = row

    def put(self, key: str, pdf: str, md5: str, grobid: str, http: int, secs: float,
            tei: bytes | None = None, error: str = "") -> None:
        """Record one GROBID attempt; on success, store its TEI first (to a
        temporary name, then renamed, so a stopped run leaves no half file)."""
        if tei is not None:
            self.dir.mkdir(parents=True, exist_ok=True)
            part = self.path(key).with_name(self.path(key).name + ".part")
            part.write_bytes(gzip.compress(tei))
            os.replace(part, self.path(key))
        row = {"key": key, "pdf": pdf, "md5": md5, "grobid": grobid,
               "settings": GROBID_REQUEST, "http": http, "secs": round(secs, 2),
               "at": datetime.now(UTC).isoformat(timespec="seconds")}
        if error:
            row["error"] = error[:300]
        self._append(row)

    def remove(self, key: str) -> None:
        """Delete a paper's stored TEI (its attachment is gone). The record
        keeps its history."""
        self.path(key).unlink(missing_ok=True)
