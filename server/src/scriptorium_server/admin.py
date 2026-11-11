"""The admin command (`scriptorium`)."""

import typer

from scriptorium_server import __version__

app = typer.Typer(no_args_is_help=True, help="Administer a Scriptorium server.")


@app.callback()
def main() -> None:
    """Administer a Scriptorium server."""


@app.command()
def version() -> None:
    """Print the server's version."""
    typer.echo(__version__)
