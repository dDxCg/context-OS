import sys

import pytest

import app.cli.daemon as daemon


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "PID_PATH", tmp_path / "ctx.pid")
    monkeypatch.setattr(daemon, "LOG_PATH", tmp_path / "ctx.log")


def _write_pid(pid: int):
    daemon.PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    daemon.PID_PATH.write_text(str(pid))


def test_ac1_start_spawns_and_writes_pid_file(monkeypatch):
    monkeypatch.setattr(daemon, "_spawn", lambda: 4242)

    pid = daemon.start()

    assert pid == 4242
    assert daemon.PID_PATH.read_text().strip() == "4242"


def test_ac2_start_refuses_when_a_live_pid_exists(monkeypatch):
    _write_pid(111)
    monkeypatch.setattr(daemon, "_is_process_alive", lambda pid: pid == 111)
    spawn_calls = []
    monkeypatch.setattr(daemon, "_spawn", lambda: spawn_calls.append(1) or 999)

    with pytest.raises(daemon.DaemonAlreadyRunningError):
        daemon.start()

    assert spawn_calls == []


def test_ac3_start_proceeds_when_pid_file_is_stale(monkeypatch):
    _write_pid(111)
    monkeypatch.setattr(daemon, "_is_process_alive", lambda pid: False)
    monkeypatch.setattr(daemon, "_spawn", lambda: 4242)

    pid = daemon.start()

    assert pid == 4242
    assert daemon.PID_PATH.read_text().strip() == "4242"


def test_ac4_status_reports_running_for_a_live_pid(monkeypatch):
    _write_pid(111)
    monkeypatch.setattr(daemon, "_is_process_alive", lambda pid: pid == 111)

    result = daemon.status()

    assert result == {"running": True, "pid": 111}


def test_ac5_status_reports_stopped_when_no_pid_file():
    result = daemon.status()

    assert result == {"running": False, "pid": None}


def test_ac5_status_reports_stopped_for_a_stale_pid(monkeypatch):
    _write_pid(111)
    monkeypatch.setattr(daemon, "_is_process_alive", lambda pid: False)

    result = daemon.status()

    assert result == {"running": False, "pid": None}


def test_ac6_stop_signals_waits_then_removes_pid_file(monkeypatch):
    _write_pid(111)
    alive = {"value": True}
    monkeypatch.setattr(daemon, "_is_process_alive", lambda pid: alive["value"])
    signals_sent = []
    monkeypatch.setattr(daemon, "_send_stop_signal", lambda pid: signals_sent.append(pid) or alive.__setitem__("value", False))
    monkeypatch.setattr(daemon, "_force_kill", lambda pid: pytest.fail("should not escalate"))

    stopped = daemon.stop(timeout=1.0)

    assert stopped is True
    assert signals_sent == [111]
    assert not daemon.PID_PATH.exists()


def test_ac7_stop_is_a_noop_when_already_stopped(monkeypatch):
    monkeypatch.setattr(daemon, "_send_stop_signal", lambda pid: pytest.fail("should not signal"))

    stopped = daemon.stop()

    assert stopped is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only creationflags")
def test_ac2_spawn_does_not_use_detached_process_on_windows(monkeypatch):
    """Spec 023 AC-2 / issue #27: DETACHED_PROCESS (and CREATE_NO_WINDOW)
    leave the child with no console at all, which breaks cross-process
    CTRL_BREAK_EVENT delivery - confirmed by experiment, see the spec."""
    import subprocess as subprocess_module

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(subprocess_module, "Popen", fake_popen)

    daemon._spawn()

    flags = captured["creationflags"]
    assert not flags & subprocess_module.DETACHED_PROCESS
    assert flags & subprocess_module.CREATE_NEW_PROCESS_GROUP
    assert captured["startupinfo"].wShowWindow == subprocess_module.SW_HIDE


def test_ec1_stop_escalates_after_timeout(monkeypatch):
    _write_pid(111)
    monkeypatch.setattr(daemon, "_is_process_alive", lambda pid: True)
    monkeypatch.setattr(daemon, "_send_stop_signal", lambda pid: None)
    killed = []
    monkeypatch.setattr(daemon, "_force_kill", lambda pid: killed.append(pid))

    stopped = daemon.stop(timeout=0.2)

    assert stopped is True
    assert killed == [111]
    assert not daemon.PID_PATH.exists()
