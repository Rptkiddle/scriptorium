"""One surveillance cycle, driven by code: harvest -> filter -> assemble ->
appraise -> report -> add -> digest.

run_cycle runs every step in a fixed order. The only LLM involvement is the
filter and appraise judgements, each batch a tool-less judge call
(scriptorium.judging). Any error in a code step ends the run as failed,
with the step and error in run.json and the traceback in work/error.txt;
nothing is retried or repaired at run time.

add_run puts one run's accepted papers into the library and records each
attempt in work/add_attempt_N.json; retry_pending re-runs the add for
earlier runs whose add failed and regenerates their digests; write_digest
condenses a run into digest.md (keeping any earlier digest in work/) and
finishes it. The step scripts (surveil_add.py,
surveil_digest.py) are thin wrappers around these functions.

The report step writes an add_pending.json marker whenever auto_add is on
and the run has accepts; a successful add removes it, a failed one (Zotero
unreachable, or any paper failing) rewrites it with the reason.
"""

import datetime
import json
import re
import traceback
from pathlib import Path

from scriptorium import judging
from scriptorium.additions import add_papers
from scriptorium.runs import finish, git_state, read_manifest, update_manifest, work_dir
from scriptorium.settings import load_settings
from scriptorium.surveillance import (
    AppraiseRetryNeeded,
    FilterRetryNeeded,
    VerdictFileError,
    _base,
    assemble_appraise,
    harvest,
    load_registry,
    read_verdicts,
    write_report,
)

PENDING = "add_pending.json"


def _entries(config, accepts: list[dict]) -> list[dict]:
    venue_coll = {
        v["name"]: v["collection_keys"][0]
        for v in load_registry(config)["venues"] if v.get("collection_keys")
    }
    entries = []
    for a in accepts:
        why = a.get("why", "")
        tags = ["surveillance"]
        if why.startswith("expansion:"):
            tags.append("expansion")
        if why.startswith("reference:"):
            tags.append("reference")
        entries.append({
            "doi": a["doi"], "title": a.get("title", ""),
            "authors": a.get("authors", ""), "abstract": a.get("abstract", ""),
            "venue": a.get("venue", ""),
            "collection_key": venue_coll.get(a.get("venue", ""), ""),
            "tags": tags, "extra": f"surveillance-why: {why}" if why else "",
        })
    return entries


def add_run(config, run_dir: Path, by: str = "self") -> dict:
    """Add one run's accepts and maintain its add_pending marker. Each
    attempt is kept as work/add_attempt_N.json (by whom, when, result or
    error); added.json holds the latest result. Returns the add_papers
    result (created / skipped_existing / failed)."""
    accepts = json.loads((run_dir / "accepted.json").read_text())
    marker = run_dir / PENDING
    work = work_dir(run_dir)
    attempt = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
               "by": by}
    attempt_file = work / f"add_attempt_{len(list(work.glob('add_attempt_*.json'))) + 1}.json"
    try:
        result = add_papers(config, _entries(config, accepts),
                            source=f"surveillance/{run_dir.name}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
        attempt_file.write_text(json.dumps({**attempt, "error": error}, indent=1))
        marker.write_text(json.dumps({"error": error}))
        raise
    attempt_file.write_text(json.dumps({**attempt, "result": result}, indent=1))
    (run_dir / "added.json").write_text(json.dumps(result, indent=1))
    if result["failed"]:
        marker.write_text(json.dumps({"failed": result["failed"]}))
    else:
        marker.unlink(missing_ok=True)
    return result


def add_summary(result: dict) -> str:
    return (f"added {len(result['created'])}, already-present {len(result['skipped_existing'])},"
            f" other-version {len(result.get('versions', {}))},"
            f" failed {len(result['failed'])}"
            + (f" — failures: {result['failed']}" if result["failed"] else "")
            + (f" — created from fallback metadata (Crossref lookup failed):"
               f" {sorted(result['unverified'])}" if result.get("unverified") else ""))


def retry_pending(config, into: Path) -> None:
    """Re-run the add for every other marked run within the catch-up window
    and record what it created, what failed, and which markers expired in
    <into>/retried.json for the current run's digest."""
    catchup = int(load_settings(config)["surveillance"]["catchup_days"])
    floor = (datetime.date.today() - datetime.timedelta(days=catchup)).isoformat()
    marked = sorted(
        d for d in (_base(config) / "runs").iterdir()
        # The current run's own marker is handled by its --run step.
        if (d / PENDING).is_file() and d.resolve() != into.resolve()
    )
    runs = [d for d in marked if d.name[:10] >= floor]
    expired = [d.name for d in marked if d.name[:10] < floor]
    retried: list[dict] = []
    failures: dict[str, str] = {}
    if not runs:
        print("no pending additions within the catch-up window")
    for name in expired:
        print(f"expired (older than {catchup} days, add manually via the viewer): {name}")
    for run_dir in runs:
        try:
            result = add_run(config, run_dir, by=into.name)
        except Exception as exc:  # noqa: BLE001 — one failure must not block the rest
            failures[run_dir.name] = f"{type(exc).__name__}: {exc}"[:200]
            print(f"retry failed {run_dir.name}: {failures[run_dir.name]}")
            continue
        if result["failed"]:
            failures[run_dir.name] = f"{len(result['failed'])} paper(s) failed"
        accepts = {a["doi"].lower(): a for a in
                   json.loads((run_dir / "accepted.json").read_text())}
        retried += [
            {"run": run_dir.name, "key": c["key"], "citation_key": c["citation_key"],
             "title": accepts.get(doi, {}).get("title", "")}
            for doi, c in result["created"].items()
        ]
        # The earlier run's digest and summary now reflect the additions.
        write_digest(run_dir, by=into.name)
        print(f"retried {run_dir.name}: {add_summary(result)}")
    (into / "retried.json").write_text(json.dumps({
        "created": retried, "failed": failures, "expired": expired,
    }, indent=1))


def _load(path: Path):
    return json.loads(path.read_text()) if path.is_file() else None


def write_digest(run_dir: Path, by: str = "self") -> tuple[Path, str]:
    """Condense the run's artifacts (the manifest's harvest section,
    accepted.json, and added.json / retried.json when the add step ran)
    into digest.md, then finish the run: "partial" while additions are
    pending, "ok" otherwise. Written every run, including quiet nights, so
    its presence confirms the run happened. A digest written earlier (before
    a retried add) is kept as work/digest_N.md. Returns the digest path and
    its one-sentence summary (also the first body line)."""
    harvested = read_manifest(run_dir).get("harvest") or {}
    accepted = _load(run_dir / "accepted.json") or []
    added = _load(run_dir / "added.json")
    retry_info = _load(run_dir / "retried.json") or {}
    retried = retry_info.get("created", [])
    pending = _load(run_dir / PENDING)

    date = harvested.get("date", "unknown-date")
    n_cand = harvested.get("candidates", 0)
    n_venues = len([v for v in harvested.get("venues", []) if v.get("new")])
    errors = [v for v in harvested.get("venues", []) if "error" in v]

    created = (added or {}).get("created", {})
    versions = (added or {}).get("versions", {})
    key_of = {doi: c for doi, c in created.items()}

    summary = f"{n_cand} candidates from {n_venues} venues, {len(accepted)} accepted"
    if added is not None:
        summary += f", {len(created)} added to the library"
    if retried:
        summary += f", plus {len(retried)} caught up from earlier runs"
    summary += "."

    lines = [f"# scriptorium digest — {date}", "", summary]

    if accepted:
        lines += ["", "## Accepted", ""]
        for a in sorted(accepted, key=lambda a: a["venue"]):
            doi = a["doi"].lower()
            tag = ""
            version = versions.get(doi, {})
            if doi in key_of:
                tag = f" `[{key_of[doi]['citation_key']}]`"
                if version:
                    tag += (f" (replaces preprint {version['matched_key']}:"
                            f" {version.get('preprint', '')})")
            elif version:
                tag = (f" (preprint of {version['matched_key']}, already in library;"
                       " recorded there)")
            elif added is not None and doi in added.get("skipped_existing", []):
                tag = " (already in library)"
            elif added is not None:
                tag = " (ADD FAILED)"
            lines += [(f"- **{a['title']}** — {a['authors']}, *{a['venue']}*."
                      f" https://doi.org/{a['doi']}{tag}"),
                      f"  {a.get('why', '')}"]
    else:
        lines += ["", "Nothing accepted; a quiet night."]

    if retried:
        lines += ["", "## Caught up from earlier runs", ""]
        lines += [f"- {r['title'][:90]} `[{r['citation_key']}]` (accepted {r['run'][:10]})"
                  for r in retried]

    attention = [f"- venue error — {v['venue']}: {v['error']}" for v in errors]
    attention += [f"- preprint {v['matched_key']} kept beside its published version"
                  f" ({v['preprint'].removeprefix('kept: ')}); trash it or keep it"
                  for doi, v in versions.items()
                  if doi in created and v.get("preprint", "").startswith("kept")]
    if added and added.get("failed"):
        attention += [f"- add failed — {doi}: {reason[:100]}"
                      for doi, reason in added["failed"].items()]
    if pending is not None:
        reason = pending.get("error") or pending.get("reason") or "failed papers"
        attention.append(f"- this run's additions are PENDING ({str(reason)[:120]});"
                         " the next run retries them")
    for name, reason in (retry_info.get("failed") or {}).items():
        attention.append(f"- retry of {name} failed: {reason[:120]}")
    for name in retry_info.get("expired") or []:
        attention.append(f"- pending additions from {name} expired (older than the"
                         " catch-up window); add them via the surveillance viewer")
    if attention:
        lines += ["", "## Attention", ""] + attention

    digest = run_dir / "digest.md"
    if digest.exists():
        work = work_dir(run_dir)
        digest.rename(work / f"digest_{len(list(work.glob('digest_*.md'))) + 1}.md")
    digest.write_text("\n".join(lines) + "\n")
    finish(run_dir, "partial" if pending is not None else "ok", summary, by=by)
    return digest, summary


# -- the cycle ---------------------------------------------------------------

def _batch_number(prompt: Path) -> int:
    return int(re.search(r"_batch_(\d+)_prompt", prompt.name).group(1))


def _usable(response: Path) -> bool:
    try:
        read_verdicts(response)
        return True
    except VerdictFileError:
        return False


def _reject(response: Path) -> None:
    rejected = response.parent / "rejected"
    rejected.mkdir(exist_ok=True)
    n = len(list(rejected.glob(f"{response.name}.*"))) + 1
    response.rename(rejected / f"{response.name}.{n}")


def _judge(config, run_dir: Path, prompts: list[Path], model: str) -> None:
    """Judge `prompts` and log every call in run.json's "judging" list.
    A reply that is not a verdict array is moved to work/rejected/ and its
    batch judged once more. A batch still without a usable reply gets an
    empty verdict list, so its candidates count as unjudged and take the
    existing one-shot retry (Filter: passed on to Appraise; Appraise:
    listed in the report as not judged)."""
    prompts = sorted(prompts, key=_batch_number)
    records = judging.judge_all(prompts, model, cwd=_base(config))
    for prompt, rec in zip(prompts, records):
        response = prompt.with_name(prompt.name.replace("_prompt.txt", "_response.json"))
        if rec["ok"] and not _usable(response):
            _reject(response)
            rec["rejudged"] = judging.judge(prompt, model, cwd=_base(config))
            if response.exists() and not _usable(response):
                _reject(response)
        if not response.exists():
            response.write_text("[]")
            rec["unjudged"] = True
    log = read_manifest(run_dir).get("judging", []) + records
    update_manifest(run_dir, judging=log)


def run_cycle(config, run_dir: Path, from_date: str | None = None) -> tuple[str, str]:
    """Run one surveillance cycle in run_dir (from_date overrides every
    venue's catch-up window). Returns the run's final status (ok, partial
    or failed) and its one-line summary."""
    step = "setup"
    try:
        settings = load_settings(config)["surveillance"]
        update_manifest(run_dir, provenance={
            **(read_manifest(run_dir).get("provenance") or git_state()),
            "claude": judging.claude_version(),
            "models": {"filter": settings["model_filter"],
                       "appraise": settings["model_appraise"]},
        })
        work = work_dir(run_dir)

        step = "harvest"
        harvest(config, run_dir, from_date=from_date)

        step = "filter"
        _judge(config, run_dir, list(work.glob("filter_batch_*_prompt.txt")),
               settings["model_filter"])

        step = "assemble"
        try:
            assemble_appraise(config, run_dir)
        except FilterRetryNeeded as exc:
            _judge(config, run_dir, [work / exc.prompt_name], settings["model_filter"])
            assemble_appraise(config, run_dir)

        step = "appraise"
        _judge(config, run_dir, list(work.glob("appraise_batch_*_prompt.txt")),
               settings["model_appraise"])

        step = "report"
        try:
            write_report(config, run_dir)
        except AppraiseRetryNeeded as exc:
            _judge(config, run_dir, [work / exc.prompt_name], settings["model_appraise"])
            write_report(config, run_dir)

        step = "add"
        if int(settings["auto_add"]):
            retry_pending(config, run_dir)
            if json.loads((run_dir / "accepted.json").read_text()):
                try:
                    print(add_summary(add_run(config, run_dir)))
                except Exception as exc:  # noqa: BLE001 — marker kept; next run retries
                    print(f"add failed (next run retries): {type(exc).__name__}: {exc}")
                    update_manifest(run_dir, add_error=f"{type(exc).__name__}: {exc}"[:300])

        step = "digest"
        _, summary = write_digest(run_dir)
        return read_manifest(run_dir)["status"], summary
    except Exception as exc:  # noqa: BLE001 — any failure ends the run, recorded
        (work_dir(run_dir) / "error.txt").write_text(traceback.format_exc())
        summary = f"failed at {step}: {type(exc).__name__}: {exc}"[:300]
        finish(run_dir, "failed", summary)
        return "failed", summary
