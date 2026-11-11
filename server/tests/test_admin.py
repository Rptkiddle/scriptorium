"""The admin command answers."""

from typer.testing import CliRunner

import scriptorium_server
from scriptorium_server.admin import app


def test_version_command_prints_the_version():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0 and result.output.strip() == scriptorium_server.__version__
