"""dedupe versions: keep one item per paper across preprint and published versions.

The library-wide counterpart of the rule the add path applies (see
scriptorium.versions): a preprint whose published version is in the library
is recorded in that item's Extra and moved to the trash; of two preprints of
one paper, the one with a preprint server's DOI is kept over arXiv over
PhilSci-Archive. A dropped item with child items (attachments, notes) is
kept and flagged, and one with no DOI or arXiv ID to record is left alone
and flagged. Folders are not changed.

Two steps, as an ingestion run:
- plan (default): writes <run>/work/plan.json and report.md; changes nothing.
- --apply <run folder>: carries out that run's plan after the user's yes.
  Each pair is checked live first (both items still in the library, the
  dropped one still a preprint, its child items), so the plan's age does
  not matter; outcomes go to <run>/work/applied.jsonl.

Usage:
    uv run python scripts/dedupe_versions.py
    uv run python scripts/dedupe_versions.py --apply <run folder>
"""

import argparse
import json
from pathlib import Path

from scriptorium.config import load_config
from scriptorium.runs import finish, new_run, work_dir
from scriptorium.versions import is_preprint, pair_action, version_pairs
from scriptorium.zotero import ZoteroReader
from scriptorium.zotero_write import ZoteroWriter


def _label(item: dict) -> str:
    kind = item.get("repository") or item.get("itemType", "")
    return f"{item['key']} ({kind}, {item.get('date', '')[:4]})"


def plan(config, reader) -> Path:
    run_dir = new_run(config, "ingestion", slug="version-check")
    rows = []
    for keep, drop, how in version_pairs(reader.library_snapshot()):
        action = pair_action(keep, drop, reader.children(drop["key"]))
        rows.append({"keep": keep["key"], "drop": drop["key"], "how": how,
                     "title": drop.get("title", ""), "keep_label": _label(keep),
                     "drop_label": _label(drop), "line": action["line"],
                     "action": action["action"]})
    (work_dir(run_dir) / "plan.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    lines = [f"# ingestion — {run_dir.name}", "",
             f"Version check (plan): {len(rows)} pair(s). Nothing changed yet.", ""]
    for r in rows:
        lines.append(f"- **{r['title']}** ({r['how']}): keep {r['keep_label']},"
                     f" drop {r['drop_label']} — {r['action']}")
    (run_dir / "report.md").write_text("\n".join(lines) + "\n")
    summary = f"plan: {len(rows)} pair(s), not applied"
    finish(run_dir, "ok", summary)
    print("\n".join(lines[4:]) or "no pairs")
    print(f"summary: {summary}\nrun folder: {run_dir}")
    return run_dir


def apply(config, reader, run_dir: Path) -> None:
    rows = json.loads((work_dir(run_dir) / "plan.json").read_text())
    writer = ZoteroWriter(config, source=f"ingestion/{run_dir.name}", reader=reader)
    counts: dict[str, int] = {}
    with (work_dir(run_dir) / "applied.jsonl").open("a") as log:
        for r in rows:
            out = {"keep": r["keep"], "drop": r["drop"]}
            keep, drop = reader.item_raw(r["keep"]), reader.item_raw(r["drop"])
            if not keep or not drop or keep.get("deleted") or drop.get("deleted"):
                out["outcome"] = "skipped: an item is no longer in the library"
            elif not is_preprint(drop):
                out["outcome"] = "skipped: the dropped item is no longer a preprint"
            else:
                action = pair_action(keep, drop, reader.children(drop["key"]))
                try:
                    if action["extra"] is not None:
                        writer.update_item(keep["key"], {"extra": action["extra"]})
                    if action["trash"]:
                        writer.delete_item(drop["key"])
                    out["outcome"] = action["action"]
                except Exception as exc:  # noqa: BLE001 — record and go on
                    out["outcome"] = f"failed: {type(exc).__name__}: {exc}"[:200]
            counts[out["outcome"].split(":")[0]] = counts.get(out["outcome"].split(":")[0], 0) + 1
            log.write(json.dumps(out, ensure_ascii=False) + "\n")
            print(f"{r['drop']} -> {r['keep']}: {out['outcome']}")
    summary = "applied: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    with (run_dir / "report.md").open("a") as report:
        report.write(f"\n{summary}\n")
    finish(run_dir, "partial" if "failed" in counts else "ok", summary)
    print(f"summary: {summary}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", metavar="RUN", help="carry out this run's plan")
    args = ap.parse_args()
    config = load_config()
    reader = ZoteroReader(config)
    if args.apply:
        apply(config, reader, Path(args.apply).expanduser())
    else:
        plan(config, reader)


if __name__ == "__main__":
    main()
