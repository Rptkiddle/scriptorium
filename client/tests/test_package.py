"""The client package: its command answers, and it never imports the server."""

from pathlib import Path

from typer.testing import CliRunner

import scriptorium_client
from scriptorium_client.cli import app


def test_version_command_prints_the_version():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0 and result.output.strip() == scriptorium_client.__version__


def test_client_never_imports_the_server_package():
    sources = Path(scriptorium_client.__file__).parent.rglob("*.py")
    assert not [p.name for p in sources if "scriptorium_server" in p.read_text()]
