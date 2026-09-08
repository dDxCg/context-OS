import time

import pytest

import app.cli.daemon as daemon


@pytest.fixture(autouse=True)
def isolated_daemon_env(tmp_path, monkeypatch):
    """One real start -> status -> stop cycle against a fully isolated
    environment - the spawned subprocess is a real `python -m vcs.runtime`,
    inheriting these env vars, so it must never touch the real dev DB/repo."""
    monkeypatch.setattr(daemon, "PID_PATH", tmp_path / "ctx.pid")
    monkeypatch.setattr(daemon, "LOG_PATH", tmp_path / "ctx.log")
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("GIT_REPO_DIR", str(tmp_path / "git-repos"))
    monkeypatch.setenv("SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("TMP_DIR", str(tmp_path / "tmp"))


def _wait_until(predicate, timeout=10.0, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_real_start_status_stop_cycle():
    pid = daemon.start()

    try:
        became_running = _wait_until(lambda: daemon.status() == {"running": True, "pid": pid})
        assert became_running, "daemon never reported running within 10s"

        stopped = daemon.stop(timeout=10.0)

        assert stopped is True
        assert daemon.status() == {"running": False, "pid": None}
        assert not daemon.PID_PATH.exists()
    finally:
        # Safety net: if an assertion above failed, don't leak a real
        # background process into the rest of the CI run.
        if daemon._is_process_alive(pid):
            daemon._force_kill(pid)
