"""The scriptorium daemon: MCP server over streamable HTTP on localhost.

One long-lived process owns the index and the embedding model; every
Claude Code session connects here. The state lives in daemon.py, the MCP
tools in tools.py and the web pages in routes.py; nothing starts until
run() is called.
"""

import logging

from scriptorium import daemon
from scriptorium.config import load_config


class QuietHealth(logging.Filter):
    """Drops the access-log line of every /health request: the menubar
    polls it every few seconds, which buried everything else in
    daemon.log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/health" not in record.getMessage()


def run(host: str = "127.0.0.1", port: int | None = None) -> None:
    cfg = load_config()
    daemon.start(cfg)
    logging.getLogger("uvicorn.access").addFilter(QuietHealth())
    from scriptorium import routes  # noqa: F401 — registers the web routes
    from scriptorium.tools import mcp

    # Host/Origin guard: rejects requests whose Host is not localhost (DNS
    # rebinding) and cross-origin browser requests, so no web page can
    # change settings or call tools. Same-origin pages and non-browser
    # clients (MCP, scripts, the menubar) send no foreign Origin.
    mcp.run(transport="http", host=host, port=port or cfg.port,
            host_origin_protection="auto")
