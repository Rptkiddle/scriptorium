"""The client command (`scriptorium-client`): setup, and the MCP server the
agent starts."""

from typing import Annotated

import typer

from scriptorium_client import __version__
from scriptorium_client import settings as client_settings
from scriptorium_client.http import Server, ServerError

app = typer.Typer(no_args_is_help=True, help="Set up and run the Scriptorium client.")


@app.callback()
def main() -> None:
    """Set up and run the Scriptorium client."""


@app.command()
def version() -> None:
    """Print the client's version."""
    typer.echo(__version__)


@app.command()
def setup(
    server: Annotated[str | None, typer.Option(help="The server's address.")] = None,
    api_key: Annotated[str | None, typer.Option(help="Your API key.")] = None,
) -> None:
    """Enter the server's address and your API key, and check that they work."""
    current = client_settings.load()
    server = server or typer.prompt("Server address", default=current["server"] or None)
    api_key = api_key or typer.prompt("API key", hide_input=True,
                                      default=current["api_key"] or None, show_default=False)
    if not server.startswith(("http://", "https://")):
        server = "https://" + server
    values = {**current, "server": server.rstrip("/"), "api_key": api_key.strip()}
    try:
        probe = Server(values)
        health = probe.health()
        probe.items(count=0)  # a request that needs the key
    except ServerError as exc:
        typer.echo(f"Not saved. {exc}", err=True)
        raise typer.Exit(1) from None
    stored = {k: values[k] for k in ("server", "api_key", "reply_chars")}
    path = client_settings.save(stored)
    typer.echo(f"Connected: {health['items']} items, {health['fulltexts']} with full text.")
    typer.echo(f"Settings saved to {path}.")


@app.command()
def serve() -> None:
    """Run the client for an agent (the agent starts this itself)."""
    from scriptorium_client.tools import build_server

    build_server().run(show_banner=False)
