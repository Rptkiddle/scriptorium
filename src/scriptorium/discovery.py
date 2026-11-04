"""Discovery: systematic search for papers beyond the library.

Core logic for the discovery spoke — see docs/discovery.md for the design
and its evidence base. Deterministic work (querying, deduplication,
abstract backfill, snowballing, prompt rendering, verdict collection,
verification, reporting) lives here; relevance judgement is made by
subagents reading rendered prompt files. Every query, count, and decision
lands in the run's prisma.json so the report is generated, not
reconstructed.

Each run follows the shared run layout (see runs.py), under
<data>/discovery/runs/<YYYY-MM-DD-HHMMSS>-<slug>/:
    run.json          manifest (question, status, summary)
    protocol.json     the confirmed search protocol
    prisma.json       the audit trail
    report.md         the PRISMA-style flow report
    includes.json     the vetted, verified include set
    work/candidates.json            every candidate ever seen, with status
                                    and provenance
    work/screen_r<N>_batch_<i>_*    screening prompts, expected ids, responses
    work/arbiter_r<N>_batch_<i>_*   arbiter prompts, expected ids, responses
"""

import json
import random
import re
import time
import unicodedata
from pathlib import Path

import requests

from scriptorium.apis import OPENALEX, S2, mailto, norm_doi, request_json, strip_jats
from scriptorium.judging import parse_verdicts
from scriptorium.runs import work_dir

S2_FIELDS = "title,abstract,year,venue,authors,externalIds"
S2_DELAY = 1.1  # 1 req/s etiquette
DATACITE = "https://api.datacite.org/dois"


def _request(session: requests.Session, method: str, url: str,
             params: dict | None = None, json_body: dict | None = None) -> dict:
    """Discovery's retry policy: unauthenticated S2 throttles hard, so more
    attempts and longer waits; a 404 is an empty result."""
    return request_json(session, method, url, params=params, json_body=json_body,
                        attempts=8, wait_429=10, wait_error=3, not_found={},
                        timeout=60)


# ---------------------------------------------------------------- candidates

def _fold(text: str) -> str:
    """Casefolded and stripped of accents, so sources that spell a name with
    or without diacritics compare equal ("Reséndez" -> "resendez")."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(c))


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(title or ""))[:80]


def _first_surname(authors: list | None) -> str:
    """Last word of the first author's name, folded; "" if unknown."""
    words = ((authors or [""])[0] or "").split()
    return _fold(words[-1]) if words else ""


def fuzzy_key(cand: dict) -> str | None:
    """Title + year + first-author surname, or None when the title
    normalises to nothing (non-Latin or missing) and would match anything."""
    title = _norm_title(cand.get("title", ""))
    if not title:
        return None
    return f"fuzzy:{title}:{cand.get('year')}:{_first_surname(cand.get('authors')) or '?'}"


def dedup_key(cand: dict) -> str:
    """DOI when present; else fuzzy title + year-bucket + first-author
    surname (year missing counts as its own bucket)."""
    if cand.get("doi"):
        return f"doi:{cand['doi']}"
    return fuzzy_key(cand) or f"untitled:{id(cand)}"


def _from_s2(p: dict, provenance: str) -> dict | None:
    if not p or not p.get("title"):
        return None
    ext = p.get("externalIds") or {}
    return {
        "doi": norm_doi(ext.get("DOI", "")),
        "title": p["title"],
        "abstract": strip_jats(p.get("abstract") or ""),
        "year": p.get("year"),
        "venue": p.get("venue") or "",
        "authors": [a.get("name", "") for a in (p.get("authors") or [])],
        "s2_id": p.get("paperId", ""),
        "openalex_id": "",
        "sources": ["s2"],
        "provenance": [provenance],
    }


def _oa_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    slots: dict[int, str] = {}
    for word, positions in inv.items():
        for pos in positions:
            slots[pos] = word
    return " ".join(slots[i] for i in sorted(slots))


def _from_openalex(w: dict, provenance: str) -> dict | None:
    if not w or not w.get("display_name"):
        return None
    loc = (w.get("primary_location") or {}).get("source") or {}
    return {
        "doi": norm_doi(w.get("doi") or ""),
        "title": w["display_name"],
        "abstract": strip_jats(_oa_abstract(w.get("abstract_inverted_index"))),
        "year": w.get("publication_year"),
        "venue": loc.get("display_name") or "",
        "authors": [a.get("author", {}).get("display_name", "")
                    for a in (w.get("authorships") or [])],
        "s2_id": "",
        "openalex_id": (w.get("ids") or {}).get("openalex", ""),
        "sources": ["openalex"],
        "provenance": [provenance],
    }


def merge_candidates(pool: dict[str, dict], incoming: list[dict]) -> int:
    """Merge into the pool keyed by dedup_key; returns how many were new.
    On collision, provenance and sources accumulate and a filled abstract
    wins over an empty one."""
    # Secondary index: the same paper can arrive with a DOI from one source
    # and without from another; match those on the fuzzy key too.
    by_fuzzy = {fk: k for k, c in pool.items() if (fk := fuzzy_key(c))}
    new = 0
    for cand in incoming:
        if cand is None:
            continue
        key = dedup_key(cand)
        fk = fuzzy_key(cand)
        held_key = key if key in pool else by_fuzzy.get(fk) if fk else None
        held = pool.get(held_key) if held_key else None
        if held is None:
            pool[key] = cand
            if fk:
                by_fuzzy.setdefault(fk, key)
            new += 1
            continue
        if cand.get("doi") and not held.get("doi"):
            # Re-key the held copy under the DOI the newcomer brings.
            held["doi"] = cand["doi"]
            pool[key] = pool.pop(held_key)
            if fk:
                by_fuzzy[fk] = key
        for f in ("provenance", "sources"):
            held[f] = sorted(set(held[f]) | set(cand[f]))
        if not held["abstract"] and cand["abstract"]:
            held["abstract"] = cand["abstract"]
        for f in ("s2_id", "openalex_id", "doi"):
            held[f] = held[f] or cand[f]
    return new


# ---------------------------------------------------------------- queries

def build_queries(protocol: dict) -> list[dict]:
    """Query formulations from the protocol's concept blocks: an OpenAlex
    strict all-blocks conjunction, OpenAlex two-block relaxations (when
    there are 3+ blocks), and natural-language / keyword variants for
    Semantic Scholar's embedding ranking."""
    blocks: list[list[str]] = protocol["concept_blocks"]
    ors = ["(" + " OR ".join(f'"{t}"' if " " in t else t for t in b) + ")"
           for b in blocks]
    queries = [{"id": "q1", "source": "openalex", "expr": " AND ".join(ors),
                "note": "strict conjunction, all blocks"}]
    if len(blocks) >= 3:
        n = 2
        for i in range(len(blocks)):
            for j in range(i + 1, len(blocks)):
                queries.append({"id": f"q{n}", "source": "openalex",
                                "expr": f"{ors[i]} AND {ors[j]}",
                                "note": f"relaxation, blocks {i + 1}+{j + 1}"})
                n += 1
    n = len(queries) + 1
    queries.append({"id": f"q{n}", "source": "s2", "expr": protocol["question"],
                    "note": "natural-language question"})
    primary = " ".join(b[0] for b in blocks)
    queries.append({"id": f"q{n + 1}", "source": "s2", "expr": primary,
                    "note": "primary terms, all blocks"})
    alts = " ".join(b[1] if len(b) > 1 else b[0] for b in blocks)
    if alts != primary:
        queries.append({"id": f"q{n + 2}", "source": "s2", "expr": alts,
                        "note": "alternate synonyms"})
    return queries


def search_s2(session: requests.Session, expr: str, cap: int,
              year_from, year_to) -> list[dict]:
    hits, offset = [], 0
    expr = re.sub(r"[^\w\s-]", " ", expr)  # S2 chokes on punctuation
    params: dict = {"query": expr, "fields": S2_FIELDS, "limit": min(100, cap)}
    if year_from or year_to:
        params["year"] = f"{year_from or ''}-{year_to or ''}"
    while len(hits) < cap:
        params["offset"] = offset
        data = _request(session, "GET", f"{S2}/paper/search", params=params)
        time.sleep(S2_DELAY)
        hits += data.get("data") or []
        if data.get("next") is None or not data.get("data"):
            break
        offset = data["next"]
    return hits[:cap]


def search_openalex(session: requests.Session, expr: str, cap: int,
                    year_from, year_to) -> list[dict]:
    filters = [f"title_and_abstract.search:{expr}"]
    if year_from or year_to:
        filters.append(f"publication_year:{year_from or 1900}-{year_to or 2100}")
    hits, cursor = [], "*"
    while len(hits) < cap and cursor:
        data = _request(session, "GET", f"{OPENALEX}/works", params={
            "filter": ",".join(filters), "per-page": min(200, cap),
            "cursor": cursor, **mailto(),
        })
        hits += data.get("results") or []
        cursor = (data.get("meta") or {}).get("next_cursor")
    return hits[:cap]


def backfill_abstracts(session: requests.Session, pool: dict[str, dict]) -> int:
    """Fill missing abstracts via the S2 batch endpoint (500 ids/call).
    Returns how many are STILL missing (title-only screening)."""
    missing = [c for c in pool.values()
               if not c["abstract"] and (c["s2_id"] or c["doi"])]
    for i in range(0, len(missing), 500):
        chunk = missing[i:i + 500]
        ids = [c["s2_id"] or f"DOI:{c['doi']}" for c in chunk]
        got = _request(session, "POST", f"{S2}/paper/batch",
                       params={"fields": "abstract"}, json_body={"ids": ids})
        time.sleep(S2_DELAY)
        if isinstance(got, list):
            for cand, p in zip(chunk, got):
                if p and p.get("abstract"):
                    cand["abstract"] = strip_jats(p["abstract"])
    return sum(1 for c in pool.values() if not c["abstract"])


# ---------------------------------------------------------------- snowball

def lex_score(cand: dict, terms: set[str]) -> int:
    text = f"{cand.get('title', '')} {cand.get('abstract', '')}".casefold()
    return sum(1 for t in terms if t in text)


def protocol_terms(protocol: dict) -> set[str]:
    return {t.casefold() for block in protocol["concept_blocks"] for t in block}


def snowball(session: requests.Session, seeds: list[str], forward_cap: int,
             terms: set[str], provenance_prefix: str) -> tuple[list[dict], dict]:
    """Backward (all references) and forward (capped, term-ranked citations)
    chasing from each seed (an S2 paper id or 'DOI:...' string)."""
    out: list[dict] = []
    stats = {"seeds": len(seeds), "backward": 0, "forward_seen": 0,
             "forward_kept": 0, "seed_errors": []}
    for seed in seeds:
        try:
            refs = _request(session, "GET", f"{S2}/paper/{seed}/references",
                            params={"fields": S2_FIELDS, "limit": 1000})
            time.sleep(S2_DELAY)
            cites = _request(session, "GET", f"{S2}/paper/{seed}/citations",
                             params={"fields": S2_FIELDS, "limit": 1000})
            time.sleep(S2_DELAY)
        except (requests.RequestException, RuntimeError) as exc:
            stats["seed_errors"].append(f"{seed}: {exc}")
            continue
        if not refs and not cites:  # a 404 comes back as {}: seed unknown to S2
            stats["seed_errors"].append(f"{seed}: not found in Semantic Scholar")
            continue
        back = [_from_s2(r.get("citedPaper"), f"{provenance_prefix}:back:{seed}")
                for r in (refs.get("data") or [])]
        back = [c for c in back if c]
        stats["backward"] += len(back)
        out += back
        fwd = [_from_s2(r.get("citingPaper"), f"{provenance_prefix}:fwd:{seed}")
               for r in (cites.get("data") or [])]
        fwd = [c for c in fwd if c]
        stats["forward_seen"] += len(fwd)
        fwd.sort(key=lambda c: -lex_score(c, terms))
        kept = fwd[:forward_cap]
        stats["forward_kept"] += len(kept)
        out += kept
    return out, stats


# ---------------------------------------------------------------- screening

SCREEN_PROMPT = """You are screening candidate papers for inclusion in a systematic literature
search. Judge each candidate ONLY against the written criteria below — not
against your own sense of interestingness, and not against any hypothesis.
Base each verdict on the candidate's title, abstract, and year alone.

Research question (context only; the criteria decide):
{question}

Inclusion criteria (a paper must plausibly satisfy ALL of these):
{include}

Exclusion criteria (any ONE of these excludes a paper):
{exclude}

Verdicts:
- "include" — the title and abstract plausibly satisfy the criteria
- "exclude" — a criterion clearly fails; state which in "why"
- "unsure" — the title and abstract cannot settle it (missing abstract,
  ambiguous scope). When in doubt between exclude and unsure, choose
  unsure — a wrong exclude loses evidence permanently.

Candidates:

{cards}

Reply with ONLY a JSON array, one object per candidate, in the exact order
given: {{"id": "...", "verdict": "include|exclude|unsure", "why": "<one line>"}}.
Every candidate must appear exactly once with its id copied verbatim.
"""


ARBITER_PROMPT = """An earlier screener could not settle the candidates below against the
criteria. You are the arbiter: decide "include" or "exclude" for each —
"unsure" is not available to you. Judge ONLY against the written criteria,
from the title, abstract, and year alone. A wrong exclude loses evidence
permanently, so exclude only when a criterion clearly fails; otherwise
include.

Research question (context only; the criteria decide):
{question}

Inclusion criteria (a paper must plausibly satisfy ALL of these):
{include}

Exclusion criteria (any ONE of these excludes a paper):
{exclude}

Candidates:

{cards}

Reply with ONLY a JSON array, one object per candidate, in the exact order
given: {{"id": "...", "verdict": "include|exclude", "why": "<one line>"}}.
Every candidate must appear exactly once with its id copied verbatim.
"""


def _card(cand: dict) -> str:
    abstract = cand["abstract"][:1500] or "(no abstract — title-only)"
    year = cand.get("year") or "n.d."
    return f"[{cand['id']}] {cand['title']} ({year})\n{abstract}"


def render_screen_prompts(run_dir: Path, round_n: int, candidates: list[dict],
                          protocol: dict, batch: int) -> int:
    """Shuffled batches (seeded per round for reproducibility); returns the
    number of batch prompt files written."""
    rng = random.Random(f"{run_dir.name}:{round_n}")
    work = work_dir(run_dir)
    order = candidates[:]
    rng.shuffle(order)
    include = "\n".join(f"- {c}" for c in protocol["include_criteria"])
    exclude = "\n".join(f"- {c}" for c in protocol["exclude_criteria"])
    n_batches = 0
    for i in range(0, len(order), batch):
        chunk = order[i:i + batch]
        n_batches += 1
        prompt = SCREEN_PROMPT.format(
            question=protocol["question"], include=include, exclude=exclude,
            cards="\n\n".join(_card(c) for c in chunk),
        )
        (work / f"screen_r{round_n}_batch_{n_batches}_prompt.txt").write_text(prompt)
        (work / f"screen_r{round_n}_batch_{n_batches}_expected.json").write_text(
            json.dumps([c["id"] for c in chunk]))
    return n_batches


def collect_verdicts(run_dir: Path, round_n: int, stage: str = "screen") -> dict:
    """Parse and validate every batch response for a round. Raises with the
    offending batch names on missing files or id mismatches."""
    verdicts: dict[str, dict] = {}
    bad, missing = [], []
    work = work_dir(run_dir)
    for expected_file in sorted(work.glob(f"{stage}_r{round_n}_batch_*_expected.json")):
        response_file = expected_file.with_name(
            expected_file.name.replace("_expected.json", "_response.json"))
        if not response_file.exists():
            missing.append(response_file.name)
            continue
        expected = json.loads(expected_file.read_text())
        got = parse_verdicts(response_file.read_text())
        if got is None:
            bad.append(f"{response_file.name} (not a JSON array)")
            continue
        if [v.get("id") for v in got] != expected:
            bad.append(response_file.name)
            continue
        # The arbiter must resolve: "unsure" is not available to it.
        allowed = ("include", "exclude") if stage == "arbiter" else (
            "include", "exclude", "unsure")
        for v in got:
            if v.get("verdict") not in allowed:
                bad.append(f"{response_file.name} (bad verdict {v.get('verdict')!r})")
                break
            verdicts[v["id"]] = {"verdict": v["verdict"], "why": v.get("why", "")}
    if missing or bad:
        raise RuntimeError(
            f"screening round {round_n}: missing {missing or 'none'};"
            f" invalid {bad or 'none'} — redispatch those batches")
    return verdicts


def library_titles(items: list[dict]) -> set[str]:
    """Normalised titles of library items (ZoteroReader.library_snapshot),
    for excluding DOI-less candidates the library already holds (DOI
    checks miss those)."""
    return {t for d in items if (t := _norm_title(d.get("title", "")))}


def already_held(cand: dict, lib_dois: set[str], lib_titles: set[str]) -> bool:
    """In the library by DOI, or (DOI-less candidates) by exact normalised title."""
    if cand.get("doi"):
        return cand["doi"] in lib_dois
    title = _norm_title(cand.get("title", ""))
    return bool(title) and title in lib_titles


# ---------------------------------------------------------------- verify

LOOKUP_FAILED = "lookup failed"


def _datacite_as_crossref(record: dict) -> dict:
    """A DataCite DOI record (arXiv, Zenodo and other DataCite-registered
    DOIs) in the Crossref shape verify_include reads; {} if empty."""
    attrs = (record.get("data") or {}).get("attributes") or {}
    if not attrs:
        return {}
    first = (attrs.get("creators") or [{}])[0]
    family = first.get("familyName") or (first.get("name") or "").split(",")[0].strip()
    return {"title": [t["title"] for t in attrs.get("titles") or [] if t.get("title")][:1],
            "issued": {"date-parts": [[attrs.get("publicationYear")]]},
            "author": [{"family": family, "sequence": "first"}] if family else []}


def verify_include(session: requests.Session, cand: dict) -> str | None:
    """The DOI's registry (Crossref, or DataCite for DOIs Crossref does not
    hold, such as arXiv's) AND Semantic Scholar must agree with the
    candidate's metadata (title, year ±1, first-author surname). Returns
    None when verified, else the failure reason. Candidates without a DOI
    cannot be verified and are failed. A reason starting with LOOKUP_FAILED
    means the sources could not be reached, not that they disagree; the
    caller leaves such a candidate for a later attempt."""
    if not cand["doi"]:
        return "no DOI — cannot verify"
    registry = "crossref"
    try:
        cr = _request(session, "GET",
                      f"https://api.crossref.org/works/{cand['doi']}",
                      params=mailto()).get("message") or {}
        if not cr:
            registry = "datacite"
            cr = _datacite_as_crossref(_request(session, "GET", f"{DATACITE}/{cand['doi']}"))
        s2 = _request(session, "GET", f"{S2}/paper/DOI:{cand['doi']}",
                      params={"fields": "title,year,authors"})
        time.sleep(S2_DELAY)
    except (requests.RequestException, RuntimeError) as exc:
        return f"{LOOKUP_FAILED}: {exc}"
    if not cr:
        return "not found in Crossref or DataCite"
    if not s2:
        return "not found in Semantic Scholar"
    title = _norm_title(cand["title"])
    if not title:
        return "title does not normalise to comparable text"
    for name, source_title in ((registry, (cr.get("title") or [""])[0]),
                               ("s2", s2.get("title", ""))):
        other = _norm_title(source_title)
        if not other:
            return f"no comparable title in {name}"
        # Prefix match either way: Crossref often stores subtitles apart.
        if not (title.startswith(other[:40]) or other.startswith(title[:40])):
            return f"title mismatch vs {name}: {source_title[:80]!r}"
    year = cand.get("year")
    cr_year = ((cr.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    for name, source_year in ((registry, cr_year), ("s2", s2.get("year"))):
        if year and source_year and abs(int(year) - int(source_year)) > 1:
            return f"year mismatch vs {name}: {source_year}"
    surname = _first_surname(cand.get("authors"))
    if surname:
        cr_first = next((a.get("family", "") for a in cr.get("author", [])
                         if a.get("sequence") == "first"),
                        (cr.get("author") or [{}])[0].get("family", ""))
        s2_first = _first_surname([a.get("name", "") for a in (s2.get("authors") or [])])
        for name, source_surname in ((registry, cr_first), ("s2", s2_first)):
            other = _fold(source_surname)
            # Containment either way: compound surnames ("da Silva") split
            # differently across sources.
            if other and surname not in other and other not in surname:
                return f"first-author mismatch vs {name}: {source_surname!r}"
    return None


# ---------------------------------------------------------------- state

def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.is_file() else default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))


def candidates_path(run_dir: Path) -> Path:
    """The run's candidate pool (every candidate ever seen), in work/."""
    return work_dir(run_dir) / "candidates.json"
