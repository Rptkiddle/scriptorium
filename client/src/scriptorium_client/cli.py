"""The client command (`scriptorium-client`)."""

import typer

from scriptorium_client import __version__

app = typer.Typer(no_args_is_help=True, help="Set up and run the Scriptorium client.")


@app.callback()
def main() -> None:
    """Set up and run the Scriptorium client."""


@app.command()
def version() -> None:
    """Print the client's version."""
    typer.echo(__version__)
