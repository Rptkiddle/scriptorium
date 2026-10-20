"""Scripts' access to the running daemon's hub tools, and the library
filter arguments the interrogation scripts share.

call_tool makes one call; Hub keeps one connection open for a script that
makes many. A tool error raises fastmcp's ToolError with the tool's
message.
"""

import argparse
import asyncio
from typing import Self

from fastmcp import Client

from scriptorium.config import load_config


def _url() -> str:
    return f"http://localhost:{load_config().port}/mcp"


class Hub:
    """One connection to the daemon: `async with Hub() as hub:
    await hub.call(name, args)`."""

    async def __aenter__(self) -> Self:
        self._client = Client(_url())
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.__aexit__(*exc)

    async def call(self, name: str, args: dict | None = None):
        return (await self._client.call_tool(name, args or {})).data


def call_tool(name: str, args: dict | None = None):
    """Call one hub tool on the running daemon and return its result."""

    async def _once():
        async with Hub() as hub:
            return await hub.call(name, args)

    return asyncio.run(_once())


def add_filter_args(ap: argparse.ArgumentParser) -> None:
    """The library filters (resolve values with hub_facets first)."""
    ap.add_argument("--creator", action="append", default=[],
                    help="Filter: creator surname (repeatable)")
    ap.add_argument("--year", action="append", type=int, default=[],
                    help="Filter: year (repeatable)")
    ap.add_argument("--year-from", type=int, default=None)
    ap.add_argument("--year-to", type=int, default=None)
    ap.add_argument("--venue", default="", help="Filter: venue")


def filters_from(args: argparse.Namespace) -> dict:
    """The hub filters dict for parsed filter arguments (empty if none)."""
    filters: dict = {}
    if args.creator:
        filters["creators"] = args.creator
    if args.year:
        filters["years"] = args.year
    if args.year_from is not None:
        filters["year_from"] = args.year_from
    if args.year_to is not None:
        filters["year_to"] = args.year_to
    if args.venue:
        filters["venue"] = args.venue
    return filters
