"""The client command (`scriptorium-client`): setup, and the MCP server the
agent starts."""

from typing import Annotated

import typer

from scriptorium_client import __version__
from scriptorium_client import settings as client_settings
from scriptorium_client.http import Server, ServerError
from scriptorium_client import notice as member_notice
from scriptorium_client.notice import NOTICE

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
    current = client_settings.load(environment=False)
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
    stored = {k: values[k] for k in ("server", "api_key", "reply_chars", "notice_accepted",
                                         "admin")}
    path = client_settings.save(stored)
    typer.echo(f"Connected: {health['items']} items, {health['fulltexts']} with full text.")
    typer.echo(f"Settings saved to {path}.\n")
    typer.echo(NOTICE)


@app.command()
def notice(
    accept: Annotated[bool, typer.Option(
        "--accept", help="Accept the notice here, for agent apps that cannot ask you.")] = False,
) -> None:
    """Show the notice members agree to at the start of each agent session."""
    import sys
    from datetime import date

    typer.echo(NOTICE)
    if not accept:
        return
    if not sys.stdin.isatty():  # the member answers, not a program
        typer.echo("\nNot recorded: run this command yourself in a terminal.", err=True)
        raise typer.Exit(1)
    typer.echo("\nAn agent app that can ask you still asks at the start of each session."
               " This acceptance counts only in agent apps that cannot.")
    if not typer.confirm("Do you accept this notice?", default=False):
        typer.echo("Not recorded.")
        raise typer.Exit(1)
    values = client_settings.load(environment=False)
    stored = {k: values[k] for k in ("server", "api_key", "reply_chars", "admin")}
    stored["notice_accepted"] = member_notice.record(date.today().isoformat())
    path = client_settings.save(stored)
    typer.echo(f"Recorded in {path}.")
    typer.echo("\nFor an agent app that keeps the client from reading that file, add this line"
               " to the environment variables of its Scriptorium connector:\n\n"
               f"  {client_settings.variable('notice_accepted')}={stored['notice_accepted']}")


@app.command()
def serve() -> None:
    """Run the client for an agent (the agent starts this itself)."""
    from scriptorium_client.tools import build_server

    build_server().run(show_banner=False)
