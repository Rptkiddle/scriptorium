"""Surveillance: daily monitoring of subscribed venues via Crossref.

Pipeline: harvest -> filter -> appraise -> report (-> add -> digest), run
in a fixed order by surveillance_cycle.run_cycle. This module holds the
stages up to the report: harvest, prompt rendering, verdict parsing,
appraise assembly and the report. The filter and appraise judgements are
tool-less Claude calls (scriptorium.judging) on the prompt files rendered
here; their verdict files are parsed here. With surveillance.auto_add off,
the report is a triage surface and additions go through the ingest skill.

Subscriptions live in Zotero itself: venue collections named with a
"[✓]"/"[✗]" prefix and the venue's exact Crossref title. The registry pins
each subscribed venue to its Crossref ISSNs (one-time resolution, user
confirmed); it doubles as the collection-placement map for ingest.

Outputs follow the shared run layout (see runs.py): state (the registry,
per-venue watermarks, a seen-DOI ledger) lives in
<data>/surveillance/state/; each run is one folder,
surveillance/runs/<YYYY-MM-DD-HHMMSS>/, with run.json, report.md, digest.md,
the add step's files (accepted, added, retried, add_pending) at its root,
and every intermediate file (prompts, the ids each batch must answer,
judge replies, verdicts) in work/. Runs are served by the daemon at /surveillance. Watermarks advance only when a run completes
through report, so missed or failed days are re-covered (back as far as
surveillance.catchup_days).
"""

import datetime
import html
import json
import random
import re
import time
from pathlib import Path

import requests

from scriptorium.abstracts import fill_abstracts, summary_line
from scriptorium.apis import (
    CROSSREF,
    OPENALEX,
    crossref_get,
    http_session,
    mailto,
    request_json,
    strip_jats,
)
from scriptorium.config import Config
from scriptorium.judging import parse_verdicts
from scriptorium.runs import read_manifest, update_manifest, work_dir
from scriptorium.settings import load_settings
from scriptorium.zotero import ZoteroReader

MEGA_WEEKLY_THRESHOLD = 50  # venues above this weekly volume filter first
SEEN_LEDGER_DAYS = 45

FILTER_PROMPT = """\
You are screening newly published papers for an academic researcher. Below
is the researcher's profile, then a list of candidate papers (title, venue,
authors).

For EACH candidate decide: could this paper plausibly matter to this
researcher's work? This is an undemanding screen for eliminating clearly
irrelevant work — keep any candidate with a plausible connection to the
profile; discard only work that is clearly outside it. Judge from the
candidate's title and venue; when a title alone cannot rule a candidate
out, keep it. The
profile is an imperfect sketch of a living research agenda, not a
specification: when a candidate sits near its edges, keep it and let the
later, more demanding stage decide. You MUST give a verdict for every
candidate; there is no unsure option.

Reply with ONLY a JSON array covering every candidate, in this form:
[{"id": "c0001", "keep": true}, {"id": "c0002", "keep": false}, ...]

=== RESEARCHER PROFILE ===
{profile}

=== CANDIDATES ===
{candidates}
"""

APPRAISE_PROMPT = """\
You are appraising newly published papers for an academic researcher. Below
is the researcher's profile, then a list of candidate papers (title, venue,
authors, abstract where available). These candidates have already passed a
coarse relevance screen.

For EACH candidate make the demanding call: is this paper HIGHLY relevant —
work the researcher would plausibly read closely, cite, or build on — or
merely peripherally relevant? Add only the highly relevant; skip the
peripheral. Judge from the candidate's substance against the profile's
research questions, not from topical adjacency.

Treat the profile as an imperfect sketch of a living research agenda, not a
specification. You are also invited to surprise the researcher: add a paper
the profile does not obviously cover when you judge it could expand their
interests — work whose ideas, methods, or findings a researcher of this
profile would be glad to have encountered. Hold such additions to the same
demanding standard (would be read closely, not merely interesting), and
begin their justification with "expansion:" so they are recognisable in the
daily report. When you accept a candidate as field-library material rather
than for direct relevance to the researcher's agenda, begin its
justification with "reference:" instead. You MUST give a verdict for every candidate; there is no
unsure option.

Reply with ONLY a JSON array covering every candidate, with a one-line
justification for every verdict, skips included. For skips:
{"id": "c0001", "add": false, "why": "..."}. For adds:
{"id": "c0002", "add": true, "why": "..."}.

=== RESEARCHER PROFILE ===
{profile}

=== CANDIDATES ===
{candidates}
"""


def _base(config: Config) -> Path:
    path = config.index_dir / "surveillance"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _norm(name: str) -> str:
    text = re.sub(r"^the\s+", "", name.casefold()).replace("&", " and ")
    return re.sub(r"[^a-z0-9]", "", text)


def _most_current(records: list[dict]) -> dict:
    """Of several Crossref journal records for one venue name, the one that
    receives new papers: most current DOIs, then most DOIs overall, then the
    first found. A journal that changed ISSN keeps a record for the old one
    (Computer Law & Security Review: 0267-3649 holds only pre-2002 papers),
    and unrelated journals can share a title, so records are never merged."""
    def counts(j: dict) -> tuple[int, int]:
        c = j.get("counts") or {}
        return c.get("current-dois") or 0, c.get("total-dois") or 0
    return max(records, key=counts)


def _resolve_venue(session: requests.Session, name: str) -> tuple[dict | None, str]:
    """Resolve a venue name to a Crossref journal record.

    Crossref's journal search mishandles ampersands and ranks ambiguous
    short names poorly, so: try query variants against Crossref looking for
    an exact normalised title match; failing that, resolve via OpenAlex's
    sources index (reliable for exact venue names, carries ISSNs) and fetch
    the Crossref journal record by ISSN. Where several records match, the
    most current one is taken (_most_current).
    """
    target = _norm(name)
    exact: dict[tuple, dict] = {}
    for query in dict.fromkeys([name, name.replace("&", "and"), name.replace("&", " ")]):
        found = crossref_get(session, f"{CROSSREF}/journals", query=query, rows=20).get("items", [])
        for j in found:
            if _norm(j.get("title", "")) == target and j.get("ISSN"):
                exact.setdefault(tuple(j["ISSN"]), j)
    if exact:
        return _most_current(list(exact.values())), "exact"
    try:
        sources = request_json(
            session, "GET", f"{OPENALEX}/sources", attempts=1,
            params={"search": name, "per-page": 10, **mailto()},
        )["results"]
    except Exception:
        sources = []
    via_openalex: dict[tuple, dict] = {}
    for s in sources:
        if _norm(s.get("display_name", "")) != target:
            continue
        for issn in s.get("issn") or []:
            try:
                j = crossref_get(session, f"{CROSSREF}/journals/{issn}")
            except Exception:
                continue
            if j.get("ISSN"):
                via_openalex.setdefault(tuple(j["ISSN"]), j)
    if via_openalex:
        return _most_current(list(via_openalex.values())), "openalex"
    return None, "none"


def issn_filter(issns: list[str]) -> str:
    """A Crossref filter matching any of a venue's ISSNs. Crossref ORs
    repeated filters of one name, so print and online ISSNs are searched in
    one query and a work carrying both comes back once."""
    return ",".join(f"issn:{issn}" for issn in dict.fromkeys(issns))


def _authors(work: dict) -> str:
    names = [a.get("family") or a.get("name", "") for a in work.get("author", [])]
    names = [n for n in names if n]
    if not names:
        return ""
    return names[0] + (" et al." if len(names) > 1 else "")


# -- registry ----------------------------------------------------------------

def _state_dir(config: Config) -> Path:
    return config.index_dir / "surveillance" / "state"


def registry_path(config: Config) -> Path:
    return _state_dir(config) / "registry.json"


def load_registry(config: Config) -> dict:
    path = registry_path(config)
    return json.loads(path.read_text()) if path.exists() else {"venues": []}


def build_registry(config: Config) -> dict:
    """Resolve every [✓] venue collection to Crossref ISSNs and measure a
    week of volume (mega classification). Returns the registry dict; the
    caller prints the table for user confirmation."""
    reader = ZoteroReader(config)
    session = http_session("surveillance")
    # Prior resolutions survive a failed re-attempt (flaky networks must not
    # blank out a previously pinned venue).
    prior = {
        v["name"]: v for v in load_registry(config).get("venues", []) if "issns" in v
    }
    subs: dict[str, list[str]] = {}
    for c in reader.collections():
        if c["name"].startswith("[✓]"):
            name = html.unescape(c["name"].removeprefix("[✓]").strip())
            subs.setdefault(name, []).append(c["key"])

    week_from = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    today = datetime.date.today().isoformat()
    venues = []
    for name, keys in sorted(subs.items()):
        row = {"name": name, "collection_keys": keys}
        try:
            match, quality = _resolve_venue(session, name)
            row["match"] = quality
            if match is None:
                row["error"] = "no exact Crossref/OpenAlex venue match"
            else:
                row["matched_title"] = match["title"]
                row["issns"] = match["ISSN"]
                week = crossref_get(
                    session,
                    f"{CROSSREF}/works",
                    filter=(f"{issn_filter(match['ISSN'])},"
                            f"from-created-date:{week_from},until-created-date:{today}"),
                    rows=0,
                )["total-results"]
                row["week_volume"] = week
                row["mega"] = week >= MEGA_WEEKLY_THRESHOLD
        except Exception as exc:  # noqa: BLE001 — keep resolving the rest
            row["error"] = str(exc)[:200]
        if "error" in row and name in prior:
            row = {**prior[name], "collection_keys": keys, "match": "cached"}
        venues.append(row)
        time.sleep(0.2)

    registry = {
        "generated": today,
        "confirmed": False,
        "venues": venues,
    }
    path = registry_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=1, ensure_ascii=False))
    return registry


# -- state -------------------------------------------------------------------

def state_path(config: Config) -> Path:
    return _state_dir(config) / "state.json"


def load_state(config: Config) -> dict:
    path = state_path(config)
    if path.exists():
        return json.loads(path.read_text())
    return {"watermarks": {}, "seen": {}, "last_run": None}


def save_state(config: Config, state: dict) -> None:
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))


# -- harvest -----------------------------------------------------------------

def harvest_from(
    watermark: str | None, today: datetime.date, catchup_days: int
) -> str:
    """Start of a venue's harvest window: its last covered day, but no
    further back than catchup_days. A venue never covered starts yesterday.
    Overlap with already-covered days is harmless — the seen ledger drops
    anything already harvested."""
    if not watermark:
        return (today - datetime.timedelta(days=1)).isoformat()
    # One day before the watermark: the watermark is a local date while
    # Crossref's index-date filter is UTC, so a run just after local
    # midnight could otherwise skip the evening before.
    resume = (datetime.date.fromisoformat(watermark) - datetime.timedelta(days=1)).isoformat()
    floor = (today - datetime.timedelta(days=catchup_days)).isoformat()
    return max(resume, floor)


def harvest(config: Config, run_dir: Path, from_date: str | None = None) -> dict:
    """Pull each registered venue's works indexed by Crossref since its
    last covered day (see harvest_from; from_date overrides this for
    deliberate longer-horizon runs), dedupe against the seen ledger, and
    render filter-stage prompt files (mega venues) plus the appraise pool
    seed (field venues). Missed nights inside the catch-up window are
    covered by the next successful run.

    The window is on Crossref's index date, not its created date: some
    publishers (OUP) register a DOI weeks before depositing the record, so
    by the time the work can be found its created date lies behind every
    window. Re-indexed older works are excluded by a created-date floor
    equal to the seen ledger's span, so a work cannot outlive its ledger
    entry and be judged twice. A venue's "detected" count includes works
    the ledger already holds.

    Candidates and filter prompts go to the run's work/ folder; the
    returned harvest summary is also stored as the manifest's "harvest"
    section.
    """
    registry = load_registry(config)
    if not registry.get("confirmed"):
        raise RuntimeError("registry not confirmed — run surveil_registry.py first")
    state = load_state(config)
    settings = load_settings(config)["surveillance"]
    session = http_session("surveillance")
    today_d = datetime.date.today()
    today = today_d.isoformat()
    catchup = int(settings["catchup_days"])
    ledger_floor = (today_d - datetime.timedelta(days=SEEN_LEDGER_DAYS)).isoformat()

    work = work_dir(run_dir)
    if (work / "candidates.json").exists():
        raise RuntimeError(
            f"{run_dir} already holds a harvest; choose a fresh run directory")
    candidates: list[dict] = []
    per_venue: list[dict] = []
    for venue in registry["venues"]:
        if "error" in venue or not venue.get("issns"):
            per_venue.append({"venue": venue["name"], "error": venue.get("error", "unresolved")})
            continue
        wm = from_date or harvest_from(
            state["watermarks"].get(venue["name"]), today_d, catchup
        )
        created_floor = min(ledger_floor, wm)
        detected = kept = 0
        try:
            cursor = "*"
            while True:
                msg = crossref_get(
                    session,
                    f"{CROSSREF}/works",
                    filter=(
                        f"{issn_filter(venue['issns'])},"
                        f"from-index-date:{wm},until-index-date:{today},"
                        f"from-created-date:{created_floor},type:journal-article"
                    ),
                    select="DOI,title,abstract,author,container-title,created",
                    rows=200,
                    cursor=cursor,
                )
                for item in msg.get("items", []):
                    doi = item.get("DOI", "").lower()
                    title = (item.get("title") or [""])[0]
                    if not doi or not title:
                        continue
                    # Corrections/retractions are registered as
                    # journal-articles but are noise for triage.
                    if re.match(
                        r"^(author |publisher )?(correction|corrigendum|erratum"
                        r"|retraction)\b",
                        title,
                        re.IGNORECASE,
                    ):
                        continue
                    detected += 1
                    if doi in state["seen"]:
                        continue
                    kept += 1
                    candidates.append(
                        {
                            "id": f"c{len(candidates) + 1:04d}",
                            "doi": doi,
                            "title": title,
                            "authors": _authors(item),
                            "venue": venue["name"],
                            "mega": venue["mega"],
                            "abstract": strip_jats(item.get("abstract", ""))[:2000],
                            "created": (item.get("created") or {}).get("date-time", ""),
                        }
                    )
                cursor = msg.get("next-cursor")
                if not cursor or len(msg.get("items", [])) < 200:
                    break
            per_venue.append(
                {"venue": venue["name"], "mega": venue["mega"], "from": wm,
                 "detected": detected, "new": kept}
            )
        except Exception as exc:  # noqa: BLE001 — one venue must not kill the run
            per_venue.append({"venue": venue["name"], "error": str(exc)[:200]})
        time.sleep(0.2)

    (work / "candidates.json").write_text(
        json.dumps(candidates, indent=1, ensure_ascii=False)
    )

    profile = _profile(settings)
    mega_pool = [c for c in candidates if c["mega"]]
    # Candidates arrive grouped by venue; shuffle so every filter batch
    # sees a mix of venues and no batch sets its own standard. Seeded by
    # the run name, so a re-render reproduces the same batches.
    random.Random(run_dir.name).shuffle(mega_pool)
    field_pool = [c for c in candidates if not c["mega"]]
    filter_batches = _render_batches(
        work, "filter", FILTER_PROMPT, profile, mega_pool,
        int(settings["filter_batch"]),
    )

    summary = {
        "date": today,
        "from_override": from_date,
        "venues": per_venue,
        "candidates": len(candidates),
        "mega_candidates": len(mega_pool),
        "field_candidates": len(field_pool),
        "filter_batches": filter_batches,
    }
    update_manifest(run_dir, harvest=summary)
    return summary


def _render_candidate(c: dict, with_abstract: bool) -> str:
    """One candidate block. The filter stage judges on title/venue/authors
    alone; abstracts enter only at the appraise stage."""
    lines = [f"[{c['id']}] {c['title']}", f"{c['venue']} · {c['authors'] or 'authors unknown'}"]
    if with_abstract:
        lines.append(f"Abstract: {c['abstract'] or '(none available)'}")
    return "\n".join(lines)


def _render_prompt(template: str, profile: str, batch: list[dict], with_abstract: bool) -> str:
    # Plain replacement, not str.format — the templates contain literal
    # JSON braces that format() would treat as fields.
    return template.replace("{profile}", profile).replace(
        "{candidates}",
        "\n\n".join(_render_candidate(c, with_abstract) for c in batch)
    )


def _profile(settings: dict) -> str:
    return settings["profile"].strip() or "(no profile configured)"


def _write_batch(work: Path, name: str, template: str, profile: str,
                 batch: list[dict], with_abstract: bool) -> None:
    """Write one batch's prompt file and, beside it, <stem>_expected.json
    listing the candidate ids the batch must return verdicts for."""
    (work / name).write_text(_render_prompt(template, profile, batch, with_abstract))
    (work / name.replace("_prompt.txt", "_expected.json")).write_text(
        json.dumps([c["id"] for c in batch]))


def _render_batches(
    work: Path, stage: str, template: str, profile: str,
    pool: list[dict], per_batch: int,
) -> int:
    with_abstract = stage == "appraise"
    batches = [pool[i : i + per_batch] for i in range(0, len(pool), per_batch)]
    for n, batch in enumerate(batches, start=1):
        _write_batch(work, f"{stage}_batch_{n}_prompt.txt", template, profile,
                     batch, with_abstract)
    return len(batches)


# -- verdict files -----------------------------------------------------------

class VerdictFileError(RuntimeError):
    """A judge's response file is not a JSON array of verdict objects."""


def read_verdicts(path: Path) -> list[dict]:
    """A judge's JSON array reply. Tolerates what LLM judges add around it
    (```json fences, a sentence before or after): the outermost [...] span
    is parsed. Raises VerdictFileError naming the file if no valid array of
    objects is found, so the orchestrator redispatches that batch."""
    verdicts = parse_verdicts(path.read_text())
    if verdicts is None:
        raise VerdictFileError(
            f"{path.name} is not a JSON array of verdicts — delete it and"
            " redispatch that batch")
    return verdicts


# -- appraise assembly -------------------------------------------------------

class FilterRetryNeeded(RuntimeError):
    """Some megajournal candidates got no filter verdict; they were
    re-rendered as one extra filter batch that must be dispatched before
    assembly can proceed."""

    def __init__(self, count: int, prompt_name: str):
        super().__init__(
            f"{count} megajournal candidates received no filter verdict;"
            f" re-rendered them as {prompt_name}, to be judged before assembly"
        )
        self.count = count
        self.prompt_name = prompt_name


def assemble_appraise(config: Config, run_dir: Path) -> dict:
    """Parse filter verdicts, merge survivors with field-venue candidates,
    fill their missing abstracts (abstracts.py), render appraise prompt
    files. Returns the assembly summary.

    Every megajournal candidate must receive a verdict. Candidates the
    filter judges skipped are re-rendered once as an extra filter batch
    (FilterRetryNeeded); any still unjudged after that go straight to
    Appraise, so none is dropped unread."""
    settings = load_settings(config)["surveillance"]
    work = work_dir(run_dir)
    candidates = json.loads((work / "candidates.json").read_text())
    by_id = {c["id"]: c for c in candidates}

    kept_ids: set[str] = set()
    judged_ids: set[str] = set()
    missing: list[str] = []
    for prompt_file in sorted(work.glob("filter_batch_*_prompt.txt")):
        response = prompt_file.with_name(
            prompt_file.name.replace("_prompt.txt", "_response.json")
        )
        if not response.exists():
            missing.append(response.name)
            continue
        for verdict in read_verdicts(response):
            judged_ids.add(verdict.get("id"))
            if verdict.get("keep") and verdict.get("id") in by_id:
                kept_ids.add(verdict["id"])
    if missing:
        raise RuntimeError(f"missing filter verdicts: {', '.join(missing)}")

    profile = _profile(settings)
    unjudged = [c for c in candidates if c["mega"] and c["id"] not in judged_ids]
    retry_marker = work / "filter_retry.json"
    if unjudged and not retry_marker.exists():
        n = len(list(work.glob("filter_batch_*_prompt.txt"))) + 1
        name = f"filter_batch_{n}_prompt.txt"
        _write_batch(work, name, FILTER_PROMPT, profile, unjudged, with_abstract=False)
        retry_marker.write_text(json.dumps([c["id"] for c in unjudged]))
        raise FilterRetryNeeded(len(unjudged), name)
    passed_unjudged = {c["id"] for c in unjudged}

    pool = [
        c for c in candidates
        if not c["mega"] or c["id"] in kept_ids or c["id"] in passed_unjudged
    ]
    # Abstracts Crossref lacks are looked up for the pool only, and written
    # back so the report, accepted.json and the add step carry them too. A
    # resumed run keeps the counts of its first fill.
    if "abstracts" not in read_manifest(run_dir):
        abstracts = fill_abstracts(pool, settings)
        (work / "candidates.json").write_text(
            json.dumps(candidates, indent=1, ensure_ascii=False)
        )
        update_manifest(run_dir, abstracts=abstracts)
    batches = _render_batches(
        work, "appraise", APPRAISE_PROMPT, profile, pool,
        int(settings["appraise_batch"]),
    )
    summary = {
        "filter_survivors": len(kept_ids),
        "unjudged_passed_to_appraise": len(passed_unjudged),
        "appraise_pool": len(pool),
        "appraise_batches": batches,
    }
    update_manifest(run_dir, assembly=summary)
    return summary


# -- report ------------------------------------------------------------------

class AppraiseRetryNeeded(RuntimeError):
    """Some appraise-pool candidates got no verdict; they were re-rendered
    as one extra appraise batch that must be dispatched before the report
    can be written."""

    def __init__(self, count: int, prompt_name: str):
        super().__init__(
            f"{count} candidates received no appraise verdict; re-rendered them"
            f" as {prompt_name}, to be judged before the report"
        )
        self.count = count
        self.prompt_name = prompt_name


def write_report(config: Config, run_dir: Path) -> Path:
    """Parse appraise verdicts, write the daily report, and update state:
    per-venue watermarks (a record of the last day each venue was covered)
    and the seen-DOI ledger. The state update happens ONLY here, and last,
    so an incomplete run leaves no trace in the ledger.

    Every appraise-pool candidate must receive a verdict: unjudged ones are
    re-rendered once as an extra batch (AppraiseRetryNeeded); any still
    unjudged after that are listed in the report and left out of the seen
    ledger. With auto_add on and accepts present, an add_pending marker is
    written before state advances, so a run that dies before its add step
    is retried by later runs."""
    manifest = read_manifest(run_dir)
    harvest_summary = manifest["harvest"]
    work = work_dir(run_dir)
    candidates = json.loads((work / "candidates.json").read_text())
    by_id = {c["id"]: c for c in candidates}
    settings = load_settings(config)["surveillance"]

    filter_kept: set[str] = set()
    for f in sorted(work.glob("filter_batch_*_response.json")):
        for v in read_verdicts(f):
            if v.get("keep"):
                filter_kept.add(v.get("id"))

    accepted: list[dict] = []
    appraised: list[dict] = []
    appraised_ids: set[str] = set()
    pool_ids: set[str] = set()
    missing = []
    prompts = sorted(work.glob("appraise_batch_*_prompt.txt"))
    for prompt_file in prompts:
        expected = prompt_file.with_name(
            prompt_file.name.replace("_prompt.txt", "_expected.json"))
        pool_ids.update(json.loads(expected.read_text()))
        response = prompt_file.with_name(
            prompt_file.name.replace("_prompt.txt", "_response.json")
        )
        if not response.exists():
            missing.append(response.name)
            continue
        for v in read_verdicts(response):
            if v.get("id") in appraised_ids:
                continue  # a retry batch can repeat an id; first verdict stands
            appraised_ids.add(v.get("id"))
            if v.get("id") in by_id:
                c = by_id[v["id"]]
                appraised.append({"id": c["id"], "add": bool(v.get("add")),
                                  "why": v.get("why", ""), "title": c["title"],
                                  "venue": c["venue"], "doi": c["doi"]})
            if v.get("add") and v.get("id") in by_id:
                accepted.append({**by_id[v["id"]], "why": v.get("why", "")})
    if missing:
        raise RuntimeError(f"missing appraise verdicts: {', '.join(missing)}")
    unjudged = [by_id[i] for i in sorted(pool_ids - appraised_ids) if i in by_id]
    retry_marker = work / "appraise_retry.json"
    if unjudged and not retry_marker.exists():
        name = f"appraise_batch_{len(prompts) + 1}_prompt.txt"
        profile = _profile(settings)
        _write_batch(work, name, APPRAISE_PROMPT, profile, unjudged, with_abstract=True)
        retry_marker.write_text(json.dumps([c["id"] for c in unjudged]))
        raise AppraiseRetryNeeded(len(unjudged), name)

    today = harvest_summary["date"]

    lines = [
        f"# surveillance — {today}",
        "",
        (f"Candidates: {harvest_summary['candidates']} new"
        f" ({harvest_summary['mega_candidates']} megajournal,"
        f" {harvest_summary['field_candidates']} field)"
        f" · accepted: {len(accepted)}"),
        "",
    ]
    if "abstracts" in manifest:
        lines += [summary_line(manifest["abstracts"]), ""]
    lines += [
        "| venue | detected | new | filtered | accepted |",
        "|---|---|---|---|---|",
    ]
    venue_rows = [v for v in harvest_summary["venues"] if "error" not in v]
    for v in sorted(venue_rows, key=lambda v: (-v["new"], v["venue"])):
        ids = [c["id"] for c in candidates if c["venue"] == v["venue"]]
        n_filter = len([i for i in ids if i in filter_kept]) if v["mega"] else None
        n_accept = len([a for a in accepted if a["venue"] == v["venue"]])
        lines.append(
            f"| {v['venue']} | {v['detected']} | {v['new']} |"
            f" {'NA' if n_filter is None else n_filter} | {n_accept} |"
        )
    errors = [v for v in harvest_summary["venues"] if "error" in v]

    if accepted:
        lines += ["", "## Accepted", ""]
        for a in sorted(accepted, key=lambda a: a["venue"]):
            lines += [
                (f"- **{a['title']}** — {a['authors']}, *{a['venue']}*."
                f" https://doi.org/{a['doi']}"),
                f"  {a['why']}",
            ]
    if unjudged:
        lines += ["", "## Not judged", "",
                  ("The appraise judges returned no verdict for these (after one"
                  " retry); they are left out of the seen ledger:"), ""]
        lines += [f"- {c['title']} — https://doi.org/{c['doi']}" for c in unjudged]
    if errors:
        lines += ["", "## Venue errors", ""]
        lines += [f"- {v['venue']}: {v['error']}" for v in errors]
    skipped = [a for a in appraised if not a["add"]]
    if skipped:
        lines += ["", "## Skipped", "",
                  "Appraised and not accepted, with the judge's reason:", ""]
        lines += [f"- {a['title']} — *{a['venue']}*. {a['why'] or '(no reason given)'}"
                  for a in sorted(skipped, key=lambda a: a["venue"])]

    report = run_dir / "report.md"
    report.write_text("\n".join(lines) + "\n")

    # Durable run outputs first, state last: a crash in between repeats the
    # run's report rather than losing its accepts.
    (run_dir / "accepted.json").write_text(
        json.dumps(accepted, indent=1, ensure_ascii=False)
    )
    (work / "appraised.json").write_text(
        json.dumps(appraised, indent=1, ensure_ascii=False)
    )
    if accepted and int(settings.get("auto_add", 0)):
        (run_dir / "add_pending.json").write_text(
            json.dumps({"reason": "awaiting the add step"}))
    update_manifest(run_dir, report={"accepted": len(accepted), "unjudged": len(unjudged)})

    unjudged_dois = {c["doi"] for c in unjudged}
    state = load_state(config)
    for v in venue_rows:
        state["watermarks"][v["venue"]] = today
    for c in candidates:
        if c["doi"] not in unjudged_dois:
            state["seen"][c["doi"]] = today
    cutoff = (
        datetime.date.today() - datetime.timedelta(days=SEEN_LEDGER_DAYS)
    ).isoformat()
    state["seen"] = {d: day for d, day in state["seen"].items() if day >= cutoff}
    state["last_run"] = {
        "date": today,
        "run": run_dir.name,
        "candidates": harvest_summary["candidates"],
        "accepted": len(accepted),
        "report": str(report),
    }
    save_state(config, state)
    return report
