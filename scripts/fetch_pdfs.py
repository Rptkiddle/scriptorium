"""fetch: download the version of record for PDF-less library papers.

Download only; nothing is written to Zotero (attach_pdfs.py attaches the
results afterwards). Targets are read live from Zotero: regular items other
than preprints with no PDF attachment, no own-work tag, whose host (the
publisher folder above their venue folder) has a script route in
scriptorium.pdf_routes, and a DOI (or, on a route that reads the item's
URL, a URL). --skip-staged leaves out items whose PDF an earlier run holds
for attaching. Hosts are taken in turn and paced; a host (publisher and DOI
prefix) that blocks five times in a row is stopped for the run. Every PDF
is checked (a real PDF, above a minimum size, the item's title on its first
pages) before it counts.

Each run is a retrieval run (<data>/retrieval/runs/<run>/): PDFs that pass
the check go to pdfs/<item key>.pdf, PDFs whose title check fails to
pdfs-review/, one line per item to fetch.jsonl, and the counts to
report.md. Re-running with --run <run folder> skips items already fetched.
Refuses to start off the VU network.

Usage:
    uv run python scripts/fetch_pdfs.py --plan
    uv run python scripts/fetch_pdfs.py [--run <run folder>] [--pilot N]
        [--publisher NAME ...] [--limit N] [--skip-staged <run folder> ...]
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
    on_vu_network,
    route_of,
)
from scriptorium.runs import finish, new_run
from scriptorium.zotero import ZoteroReader

KEEP = ("ok", "title_mismatch")  # outcomes a re-run does not repeat


def select_targets(records, tree, urls=None, staged=frozenset()):
    """(targets by publisher, counts of everything left out and why).
    `urls` maps item keys to their URL field, which lets items without a DOI
    through on a URL resolver; `staged` holds keys whose PDF waits in an
    earlier run, still to be attached."""
    urls = urls or {}
    targets, left_out = defaultdict(list), Counter()
    for r in records:
        if r.item_type == "preprint":
            left_out["preprint (preprint PDFs are not fetched)"] += 1
            continue
        if r.item_key in staged:
            left_out["PDF staged in an earlier run"] += 1
            continue
        if r.pdf_status != "no_pdf":
            left_out["has a PDF attachment" if r.pdf_status == "ok"
                     else f"attachment file missing ({r.pdf_status})"] += 1
            continue
        if "own-work" in r.tags:
            left_out["own-work"] += 1
            continue
        publisher, venue, route = route_of(r, tree)
        if route.group != "script":
            left_out[f"group {route.group}" + (f": {publisher}" if publisher else "")] += 1
            continue
        if not r.doi and not (route.resolver in URL_RESOLVERS and urls.get(r.item_key)):
            left_out[f"no DOI: {publisher}"] += 1
            continue
        targets[publisher].append((r, venue, route))
    return targets, left_out


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


def write_report(run_dir: Path, log: dict, stopped: set, left_out: Counter) -> Counter:
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
    ap.add_argument("--skip-staged", action="append", default=[], metavar="RUN",
                    help="leave out items whose PDF that run already holds (not yet attached)")
    args = ap.parse_args()

    cfg = load_config()
    reader = ZoteroReader(cfg)
    tree = host_tree(reader.collections())
    urls = {d["key"]: d.get("url", "") for d in reader.library_snapshot()}
    staged = {k for run in args.skip_staged for k, e in read_log(Path(run).expanduser() / "fetch.jsonl").items()
              if e["outcome"] in KEEP}
    targets, left_out = select_targets(reader.iter_papers(), tree, urls, staged)
    if args.publisher:
        targets = {p: v for p, v in targets.items() if p in args.publisher}
    if args.pilot:
        targets = pilot(targets, args.pilot)

    if args.plan:
        for pub, items in sorted(targets.items(), key=lambda kv: -len(kv[1])):
            print(f"{len(items):6}  {pub}")
        print(f"{sum(len(v) for v in targets.values()):6}  to fetch")
        for k, v in sorted(left_out.items()):
            print(f"{v:6}  left out: {k}")
        return

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    ip = session.get("https://api.ipify.org", timeout=30).text.strip()
    print(f"public IP {ip}")
    if not on_vu_network(ip):
        raise SystemExit("not on the VU network (145.108.0.0/16): closed content would fail; stopping")

    run_dir = Path(args.run).expanduser() if args.run else new_run(cfg, "retrieval", slug="fetch")
    pdf_dir, review_dir = run_dir / "pdfs", run_dir / "pdfs-review"
    pdf_dir.mkdir(exist_ok=True)
    review_dir.mkdir(exist_ok=True)
    log_path = run_dir / "fetch.jsonl"
    log = read_log(log_path)
    queue = [x for x in interleave(targets) if log.get(x[0].item_key, {}).get("outcome") not in KEEP]
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
        totals = write_report(run_dir, log, stopped, left_out)
        summary = (f"{totals['ok']} PDFs of {len(log)} items; "
                   + ", ".join(f"{k} {v}" for k, v in sorted(totals.items()) if k != "ok"))
        finish(run_dir, "ok" if totals["ok"] == len(log) else "partial", summary,
               retrieval={"outcomes": dict(totals), "stopped": sorted(stopped)})
        print(summary)


if __name__ == "__main__":
    main()
