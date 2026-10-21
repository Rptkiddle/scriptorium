"""fetch: download the version of record for PDF-less library papers.

Download only; nothing is written to Zotero (attach_pdfs.py attaches the
results afterwards). Targets are read live from Zotero: regular items other
than preprints with no PDF attachment, no own-work tag, whose host (the
publisher folder above their venue folder) has a script route in
scriptorium.pdf_routes, and a DOI (or, on a route that reads the item's
URL, a URL). Earlier runs are read for what they found, and the targets
are taken in two tiers: first the papers no run has tried, then, only with
--retry, the papers an earlier run tried and missed (no URL, not found, an
HTML page, a block), so no miss is ever final. A paper whose PDF an earlier
run still holds (staged, or in pdfs-review/) is left out. --keys /
--keys-from limit the run to named items (e.g. the papers an ingest run
just added), under the same rules, and report what happened to each.
Hosts are taken in turn and paced; a host (publisher and DOI prefix) that
blocks five times in a row is stopped for the run. Every PDF
is checked (a real PDF, above a minimum size, the item's title on its first
pages) before it counts.

Each run is a retrieval run (<data>/retrieval/runs/<run>/): PDFs that pass
the check go to pdfs/<item key>.pdf, PDFs whose title check fails to
pdfs-review/, one line per item to fetch.jsonl, and the counts to
report.md. Re-running with --run <run folder> skips items already fetched.
Run it from a network with the institution's subscriptions: elsewhere,
closed content comes back as a web page or a refusal (a miss, which a later
--retry can take again).

Usage:
    uv run python scripts/fetch_pdfs.py --plan [--keys KEY ... | --keys-from added.json]
    uv run python scripts/fetch_pdfs.py [--run <run folder>] [--pilot N]
        [--publisher NAME ...] [--limit N] [--retry]
        [--keys KEY ... | --keys-from <ingestion or surveillance run>/added.json]
"""

import argparse
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import requests

from scriptorium.config import load_config
from scriptorium.pdf_routes import (
    BLOCK_BREAKER,
    URL_RESOLVERS,
    USER_AGENT,
    Pacer,
    candidate_urls,
    fetch_pdf,
    host_tree,
    interleave,
    review_decisions,
    route_of,
)
from scriptorium.runs import arm_dir, finish, new_run
from scriptorium.zotero import ZoteroReader

KEEP = ("ok", "title_mismatch")  # outcomes a re-run does not repeat
UNTRIED = ("host_stopped",)  # outcomes of items a run never requested
TO_FETCH = "to fetch"


def earlier_outcomes(runs_dir: Path, current: Path | None = None) -> dict[str, tuple[str, str]]:
    """Item key -> (finding, run name) from earlier fetch runs. The finding
    is "staged" (a PDF that passed its check, or awaits review, and whose
    file is still on disk) or the outcome of a miss (no_url, not_found,
    html, blocked, ..., or "rejected in review": a held PDF the user
    rejected, so the same wrong file is not fetched again by default). Items a run never requested (host_stopped) and
    staged files since deleted count as untried. `current` (a run being
    resumed) is not an earlier run. Runs are read oldest first, so the
    latest finding wins."""
    found: dict[str, tuple[str, str]] = {}
    for log_path in sorted(runs_dir.glob("*/fetch.jsonl")):
        run = log_path.parent
        if current is not None and run.resolve() == current.resolve():
            continue
        decisions = review_decisions(run)
        for key, e in read_log(log_path).items():
            outcome = e.get("outcome", "")
            if outcome == "title_mismatch" and decisions.get(key, {}).get("decision") == "reject":
                found[key] = ("rejected in review", run.name)
            elif outcome in KEEP:
                folder = "pdfs" if outcome == "ok" else "pdfs-review"
                if (run / folder / f"{key}.pdf").is_file():
                    found[key] = ("staged", run.name)
            elif outcome and outcome not in UNTRIED:
                found[key] = (outcome, run.name)
    return found


def select_targets(records, tree, urls=None, tried=None, retry=False):
    """(tier 1, tier 2, counts of everything left out and why, the reason
    for each item). Tier 1 holds the papers no earlier run tried, tier 2
    those an earlier run tried and missed; both by publisher. `urls` maps
    item keys to their URL field, which lets items without a DOI through on
    a URL resolver; `tried` is earlier_outcomes(). `retry` only words the
    reasons: whether tier 2 is fetched is the caller's choice."""
    urls, tried = urls or {}, tried or {}
    targets, retries = defaultdict(list), defaultdict(list)
    left_out, reasons = Counter(), {}
    for r in records:
        publisher, venue, route = route_of(r, tree)
        finding, run = tried.get(r.item_key, ("", ""))
        if r.item_type == "preprint":
            reason = "preprint (preprint PDFs are not fetched)"
        elif r.pdf_status != "no_pdf":
            reason = ("has a PDF attachment" if r.pdf_status == "ok"
                      else f"attachment file missing ({r.pdf_status})")
        elif finding == "staged":
            reason = "PDF staged in an earlier run"
        elif "own-work" in r.tags:
            reason = "own-work"
        elif route.group != "script":
            reason = f"group {route.group}" + (f": {publisher}" if publisher else "")
        elif not r.doi and not (route.resolver in URL_RESOLVERS and urls.get(r.item_key)):
            reason = f"no DOI: {publisher}"
        elif finding:
            retries[publisher].append((r, venue, route))
            reasons[r.item_key] = (f"tried before: {finding} (run {run}); "
                                   + ("retried in this run" if retry else "held back, use --retry"))
            continue
        else:
            targets[publisher].append((r, venue, route))
            reasons[r.item_key] = TO_FETCH
            continue
        left_out[reason] += 1
        reasons[r.item_key] = reason
    return targets, retries, left_out, reasons


def named_keys(keys: list[str], keys_from: str | None) -> list[str]:
    """Item keys named with --keys, plus the created items of an ingestion
    or surveillance run's added.json given with --keys-from."""
    named = list(keys)
    if keys_from:
        added = json.loads(Path(keys_from).expanduser().read_text())
        named += [c["key"] for c in added.get("created", {}).values()]
    return list(dict.fromkeys(named))


def pilot(targets: dict, n: int, seed: int = 20261015) -> dict:
    """About n items spread evenly over the publishers, chosen at random."""
    rng = random.Random(seed)
    per = math.ceil(n / max(1, len(targets)))
    picked = {p: rng.sample(items, min(per, len(items))) for p, items in targets.items()}
    keep = {id(x) for x in interleave(picked)[:n]}
    return {p: [x for x in items if id(x) in keep] for p, items in picked.items()}


def read_log(path: Path) -> dict:
    last = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            last[entry["key"]] = entry
    return last


def write_report(run_dir: Path, log: dict, stopped: set, left_out: Counter,
                 named: dict | None = None) -> Counter:
    by_pub = defaultdict(Counter)
    for e in log.values():
        by_pub[e["publisher"]][e["outcome"]] += 1
    totals = Counter(e["outcome"] for e in log.values())
    outcomes = sorted(totals)
    lines = [f"# fetch — {run_dir.name}", "",
             f"Items tried: {len(log)} · " + ", ".join(f"{o} {totals[o]}" for o in outcomes), "",
             "| Publisher | " + " | ".join(outcomes) + " |",
             "|---|" + "---|" * len(outcomes)]
    for pub in sorted(by_pub):
        lines.append(f"| {pub} | " + " | ".join(str(by_pub[pub][o]) for o in outcomes) + " |")
    if stopped:
        lines += ["", "Stopped after repeated blocks: " + ", ".join(sorted(stopped))]
    checked = totals["ok"] + totals["title_mismatch"]
    if checked:
        rate = f"{totals['ok']}/{checked} = {totals['ok'] / checked:.0%}"
        lines += ["", f"Title-match rate (ok / (ok + title_mismatch)): {rate}"]
    samples = {}
    for e in log.values():
        if e["outcome"] == "ok":
            samples.setdefault(e["publisher"], e)
    if samples:
        lines += ["", "## One fetched PDF per publisher, to open by eye", ""]
        lines += [f"- {p}: pdfs/{e['key']}.pdf — {e['title'][:80]}" for p, e in sorted(samples.items())]
    review = [e for e in log.values() if e["outcome"] == "title_mismatch"]
    if review:
        lines += ["", "## Title check failed (files in pdfs-review/)", ""]
        lines += [f"- {e['key']} {e['title'][:90]} — {e['url']}" for e in review]
    if named:
        lines += ["", "## Named items", ""]
        lines += [f"- {k}: {log[k]['outcome'] if k in log else reason}"
                  for k, reason in named.items()]
    lines += ["", "## Left out of this run", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(left_out.items())]
    (run_dir / "report.md").write_text("\n".join(lines) + "\n")
    return totals


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="resume this run folder (default: a new run)")
    ap.add_argument("--plan", action="store_true", help="count targets per publisher; fetch nothing")
    ap.add_argument("--pilot", type=int, default=0, help="about N items spread over the publishers")
    ap.add_argument("--publisher", action="append", default=[], help="only these publisher folders")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--keys", nargs="+", default=[], metavar="KEY",
                    help="only these items (Zotero item keys)")
    ap.add_argument("--keys-from", metavar="ADDED_JSON",
                    help="only the items an ingestion or surveillance run created (its added.json)")
    ap.add_argument("--retry", action="store_true",
                    help="also fetch, after the never-tried ones, items an earlier run tried and missed")
    args = ap.parse_args()

    cfg = load_config()
    reader = ZoteroReader(cfg)
    tree = host_tree(reader.collections())
    urls = {d["key"]: d.get("url", "") for d in reader.library_snapshot()}
    tried = earlier_outcomes(arm_dir(cfg, "retrieval") / "runs",
                             Path(args.run).expanduser() if args.run else None)
    records = reader.iter_papers()
    keys = named_keys(args.keys, args.keys_from)
    if keys:
        wanted = set(keys)
        records = [r for r in records if r.item_key in wanted]
    targets, retries, left_out, reasons = select_targets(records, tree, urls, tried, args.retry)
    named = {k: reasons.get(k, "not in the library (or not a top-level item)") for k in keys}
    if args.publisher:
        targets = {p: v for p, v in targets.items() if p in args.publisher}
        retries = {p: v for p, v in retries.items() if p in args.publisher}
    if args.pilot:
        targets, retries = pilot(targets, args.pilot), pilot(retries, args.pilot)

    if args.plan:
        print("Never tried (fetched by default):")
        for pub, items in sorted(targets.items(), key=lambda kv: -len(kv[1])):
            print(f"{len(items):6}  {pub}")
        print(f"{sum(len(v) for v in targets.values()):6}  in all")
        print("Tried before and missed (fetched only with --retry, after the above):")
        missed: Counter = Counter()
        last: dict[str, str] = {}
        for r, _, _ in (x for items in retries.values() for x in items):
            finding, run = tried[r.item_key]
            missed[finding] += 1
            last[finding] = max(last.get(finding, ""), run[:10])
        for finding, n in missed.most_common():
            print(f"{n:6}  {finding} (last tried {last[finding]})")
        print(f"{sum(missed.values()):6}  in all")
        print("Left out:")
        for k, v in sorted(left_out.items()):
            print(f"{v:6}  {k}")
        for k, reason in named.items():
            print(f"  {k}: {reason}")
        return

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    run_dir = Path(args.run).expanduser() if args.run else new_run(cfg, "retrieval", slug="fetch")
    pdf_dir, review_dir = run_dir / "pdfs", run_dir / "pdfs-review"
    pdf_dir.mkdir(exist_ok=True)
    review_dir.mkdir(exist_ok=True)
    log_path = run_dir / "fetch.jsonl"
    log = read_log(log_path)
    # Never-tried papers first, so an interrupted run has spent its time on them.
    tiers = interleave(targets) + (interleave(retries) if args.retry else [])
    queue = [x for x in tiers if log.get(x[0].item_key, {}).get("outcome") not in KEEP]
    if args.limit:
        queue = queue[: args.limit]
    print(f"run {run_dir}\n{len(queue)} items to fetch")

    pacer, streak, stopped = Pacer(), Counter(), set()
    try:
        with log_path.open("a") as out:
            for i, (rec, venue, route) in enumerate(queue, 1):
                publisher = route_of(rec, tree)[0]
                # Blocks are counted per publisher and DOI prefix: one folder
                # can hold papers served by different hosts (AIES under AAAI
                # has ACM DOIs).
                streak_key = f"{publisher} {rec.doi.split('/')[0]}".strip()
                entry = {"key": rec.item_key, "doi": rec.doi, "title": rec.title,
                         "publisher": publisher, "venue": venue, "url": "", "bytes": 0}
                if streak_key in stopped:
                    entry["outcome"] = "host_stopped"
                else:
                    try:
                        found = candidate_urls(session, rec, route.resolver, urls.get(rec.item_key, ""))
                    except Exception as exc:  # noqa: BLE001 — one item's lookup, not the run
                        found, entry["outcome"] = [], f"lookup_{type(exc).__name__}"
                    if found:
                        outcome, url, pdf = fetch_pdf(session, found, rec.title, pacer, route.delay)
                        entry.update(outcome=outcome, url=url)
                        if pdf:
                            folder = pdf_dir if outcome == "ok" else review_dir
                            (folder / f"{rec.item_key}.pdf").write_bytes(pdf)
                            entry.update(bytes=len(pdf), sha256=hashlib.sha256(pdf).hexdigest())
                    elif "outcome" not in entry:
                        entry["outcome"] = "no_url"
                    streak[streak_key] = streak[streak_key] + 1 if entry["outcome"] == "blocked" else 0
                    if streak[streak_key] >= BLOCK_BREAKER:
                        stopped.add(streak_key)
                        print(f"  {streak_key}: {BLOCK_BREAKER} blocks in a row, stopped for this run")
                entry["at"] = datetime.now(UTC).isoformat(timespec="seconds")
                out.write(json.dumps(entry, ensure_ascii=False) + "\n")
                out.flush()
                log[rec.item_key] = entry
                print(f"{i:5}/{len(queue)} {entry['outcome']:15} {publisher[:28]:28} {rec.doi}", flush=True)
    finally:
        totals = write_report(run_dir, log, stopped, left_out, named)
        summary = (f"{totals['ok']} PDFs of {len(log)} items; "
                   + ", ".join(f"{k} {v}" for k, v in sorted(totals.items()) if k != "ok"))
        finish(run_dir, "ok" if totals["ok"] == len(log) else "partial", summary,
               retrieval={"outcomes": dict(totals), "stopped": sorted(stopped)})
        print(summary)


if __name__ == "__main__":
    main()
