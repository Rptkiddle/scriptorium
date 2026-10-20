"""surveillance, registry: resolve [✓] venue collections to Crossref ISSNs.

Prints the resolution table for user confirmation. Re-run whenever
subscriptions change in Zotero (tick/untick venue folders). Harvest refuses
an unconfirmed registry: after the user approves the table, run again with
--confirm to stamp it.

Usage:
    uv run python scripts/surveil_registry.py [--confirm]
"""

import argparse
import json

from scriptorium.config import load_config
from scriptorium.surveillance import build_registry, load_registry, registry_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="stamp the existing registry as user-confirmed")
    args = ap.parse_args()
    cfg = load_config()

    if args.confirm:
        registry = load_registry(cfg)
        if not registry.get("venues"):
            raise SystemExit("no registry to confirm — run without --confirm first")
        registry["confirmed"] = True
        registry_path(cfg).write_text(json.dumps(registry, indent=1, ensure_ascii=False))
        print(f"registry confirmed ({len(registry['venues'])} venues)")
        return

    registry = build_registry(cfg)
    ok = [v for v in registry["venues"] if "error" not in v]
    print(f"{'venue':<62.62} {'match':<6} {'wk':>5}  mega  matched title")
    for v in registry["venues"]:
        if "error" in v:
            print(f"{v['name']:<62.62} ERROR: {v['error']}")
        else:
            print(
                f"{v['name']:<62.62} {v['match']:<6} {v['week_volume']:>5} "
                f"{'mega' if v['mega'] else '    '}  "
                f"{v['matched_title'] if v['matched_title'] != v['name'] else ''}"
            )
    print(f"\nresolved {len(ok)} of {len(registry['venues'])} venues")
    print("review the table; then stamp with: "
          "uv run python scripts/surveil_registry.py --confirm")



if __name__ == "__main__":
    main()
