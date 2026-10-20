"""interrogation skill, Step 1: facet lookup for filter resolution.

Prints the index's actual metadata values (with paper counts) matching a
substring, so filter values are read from the library rather than guessed.

Usage:
    uv run python scripts/interrogate_facets.py --field creators --contains trilling
    uv run python scripts/interrogate_facets.py --field venues --contains annals
    uv run python scripts/interrogate_facets.py --field years
"""

import argparse

from fastmcp.exceptions import ToolError

from scriptorium.hub_client import call_tool


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--field", required=True, choices=["creators", "venues", "years"])
    ap.add_argument("--contains", default="", help="Case-insensitive substring filter")
    args = ap.parse_args()

    try:
        facets = call_tool("hub_facets", {"field": args.field, "contains": args.contains})
    except ToolError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    if not facets:
        print(f"no {args.field} matching {args.contains!r}")
        return
    for value, count in facets.items():
        print(f"{value}  ({count} paper{'s' if count != 1 else ''})")


if __name__ == "__main__":
    main()
