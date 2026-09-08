from typer.testing import CliRunner

import app.cli.app as cli_app
import app.cli.daemon as daemon

runner = CliRunner()


def test_daemon_start_prints_pid(monkeypatch):
    monkeypatch.setattr(daemon, "start", lambda: 4242)

    result = runner.invoke(cli_app.cli, ["daemon", "start"])

    assert result.exit_code == 0
    assert "4242" in result.output


def test_daemon_start_reports_already_running(monkeypatch):
    def raise_already_running():
        raise daemon.DaemonAlreadyRunningError(111)

    monkeypatch.setattr(daemon, "start", raise_already_running)

    result = runner.invoke(cli_app.cli, ["daemon", "start"])

    assert result.exit_code == 1
    assert "111" in result.output


def test_daemon_stop_prints_stopped(monkeypatch):
    monkeypatch.setattr(daemon, "stop", lambda: True)

    result = runner.invoke(cli_app.cli, ["daemon", "stop"])

    assert result.exit_code == 0
    assert "stopped" in result.output


def test_daemon_stop_prints_not_running(monkeypatch):
    monkeypatch.setattr(daemon, "stop", lambda: False)

    result = runner.invoke(cli_app.cli, ["daemon", "stop"])

    assert result.exit_code == 0
    assert "not running" in result.output


def test_daemon_status_prints_running_pid(monkeypatch):
    monkeypatch.setattr(daemon, "status", lambda: {"running": True, "pid": 4242})

    result = runner.invoke(cli_app.cli, ["daemon", "status"])

    assert result.exit_code == 0
    assert "4242" in result.output


def test_daemon_status_prints_stopped(monkeypatch):
    monkeypatch.setattr(daemon, "status", lambda: {"running": False, "pid": None})

    result = runner.invoke(cli_app.cli, ["daemon", "status"])

    assert result.exit_code == 0
    assert "stopped" in result.output
