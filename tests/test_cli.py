"""CLI commands that open the index refuse while the daemon owns it."""

from typer.testing import CliRunner

from scriptorium import cli


def test_sync_refuses_while_the_daemon_runs(monkeypatch):
    monkeypatch.setattr(cli, "daemon_running", lambda port, timeout=1.0: True)
    runner = CliRunner()
    for args in (["sync"],):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 1
        assert "daemon is running" in result.output
