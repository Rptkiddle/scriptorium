"""The client command (`scriptorium-client`): setup, and the MCP server the
agent starts."""

from typing import Annotated

import typer

from scriptorium_client import __version__
from scriptorium_client import login as member_login
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


DEVICE_HELP = ("Log in on another device: for a machine without a browser, such as a"
               " session over SSH.")


def _log_in(server: str, details: dict, device: bool) -> None:
    """Log the member in at the server's login provider, check that the
    server accepts the login, and save it."""
    provider = member_login.Provider(details["issuer"], details["client_id"],
                                     details.get("scope") or "openid offline_access")
    try:
        tokens = (member_login.device_login(provider, show=typer.echo) if device
                  else member_login.browser_login(provider, show=typer.echo))
        if not tokens.get("refresh_token"):
            raise member_login.LoginError("The login provider returned no token to stay logged"
                                          " in with; ask the server's admin.")
        session = member_login.Session(provider, tokens["refresh_token"])
        session.accept(tokens)
        current = client_settings.load(environment=False)
        # The member's API key, if they have one, goes along this once, so the
        # server attaches the login to the member they already are.
        health = Server({**current, "server": server}, session=session).health(carry_key=True)
    except (member_login.LoginError, ServerError) as exc:
        typer.echo(f"Not logged in. {exc}", err=True)
        raise typer.Exit(1) from None
    path = client_settings.update({"server": server, "issuer": provider.issuer,
                                   "client_id": provider.client_id,
                                   "refresh_token": tokens["refresh_token"]})
    typer.echo(f"Logged in. Connected: {health['items']} items, {health['fulltexts']} with"
               " full text.")
    typer.echo(f"Settings saved to {path}.")


@app.command()
def setup(
    server: Annotated[str | None, typer.Option(help="The server's address.")] = None,
    api_key: Annotated[str | None, typer.Option(
        help="Your API key, for a server that uses keys.")] = None,
    device: Annotated[bool, typer.Option("--device", help=DEVICE_HELP)] = False,
) -> None:
    """Enter the server's address and log in (or, for a server that uses
    API keys, enter your key), and check that it works."""
    current = client_settings.load(environment=False)
    server = server or typer.prompt("Server address", default=current["server"] or None)
    if not server.startswith(("http://", "https://")):
        server = "https://" + server
    server = server.rstrip("/")
    try:
        details = Server({"server": server}).login_details()
    except ServerError as exc:
        typer.echo(f"Not saved. {exc}", err=True)
        raise typer.Exit(1) from None
    if details.get("issuer") and not api_key:
        _log_in(server, details, device)
        typer.echo("\n" + NOTICE)
        return
    api_key = api_key or typer.prompt("API key", hide_input=True,
                                      default=current["api_key"] or None, show_default=False)
    values = {"server": server, "api_key": api_key.strip()}
    try:
        probe = Server(values)
        health = probe.health()
        probe.items(count=0)  # a request that needs the key
    except ServerError as exc:
        typer.echo(f"Not saved. {exc}", err=True)
        raise typer.Exit(1) from None
    path = client_settings.update(values)
    typer.echo(f"Connected: {health['items']} items, {health['fulltexts']} with full text.")
    typer.echo(f"Settings saved to {path}.\n")
    typer.echo(NOTICE)


@app.command()
def login(
    device: Annotated[bool, typer.Option("--device", help=DEVICE_HELP)] = False,
) -> None:
    """Log in at the server's login provider (again, or for the first time
    on a server that used API keys before)."""
    server = client_settings.load()["server"]
    if not server:
        typer.echo("No server is set up yet. Run `scriptorium-client setup`.", err=True)
        raise typer.Exit(1)
    try:
        details = Server({"server": server}).login_details()
    except ServerError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    if not details.get("issuer"):
        typer.echo("This server uses API keys, not a login. Run `scriptorium-client setup`"
                   " to enter yours.", err=True)
        raise typer.Exit(1)
    _log_in(server, details, device)


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
    stored = {"notice_accepted": member_notice.record(date.today().isoformat())}
    path = client_settings.update(stored)
    typer.echo(f"Recorded in {path}.")
    typer.echo("\nFor an agent app that keeps the client from reading that file, add this line"
               " to the environment variables of its Scriptorium connector:\n\n"
               f"  {client_settings.variable('notice_accepted')}={stored['notice_accepted']}")


@app.command()
def serve() -> None:
    """Run the client for an agent (the agent starts this itself)."""
    from scriptorium_client.tools import build_server

    build_server().run(show_banner=False)
