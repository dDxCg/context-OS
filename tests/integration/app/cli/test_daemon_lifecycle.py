import os
import subprocess
import sys
import time

import pytest

import app.cli.daemon as daemon
from utils.helper import PROJECT_ROOT


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


_LIFECYCLE_STEP = """
import sys
sys.path.insert(0, {src!r})
import app.cli.daemon as daemon
from pathlib import Path
daemon.PID_PATH = Path({pid_path!r})
daemon.LOG_PATH = Path({log_path!r})
print({body})
"""


def _run_step(body: str, pid_path, log_path) -> subprocess.CompletedProcess:
    script = _LIFECYCLE_STEP.format(src=str(PROJECT_ROOT / "src"), pid_path=str(pid_path), log_path=str(log_path), body=body)
    return subprocess.run(
        [sys.executable, "-c", script], env={**os.environ},
        capture_output=True, text=True, timeout=15,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="issue #27 is Windows-specific (CTRL_BREAK_EVENT)")
def test_ac1_stop_from_a_separate_process_does_not_raise(tmp_path):
    """Spec 023 AC-1 / issue #27: start() and stop() must each run as their
    own OS process, neither an ancestor of the other - exactly how the real
    `ctx daemon start` then later `ctx daemon stop` CLI invocations work.
    A stop() subprocess spawned directly by *this* test process (a child,
    not a sibling) does not reproduce the bug - Windows apparently grants
    a creator process's descendants the same CTRL_BREAK_EVENT rights as the
    creator itself, which silently defeated an earlier version of this test.
    Only two genuinely unrelated processes (the starter already exited by
    the time the stopper runs, matching real CLI usage) reproduce
    `OSError: [WinError 87]`."""
    pid_path = tmp_path / "ctx.pid"
    log_path = tmp_path / "ctx.log"

    start_result = _run_step("daemon.start()", pid_path, log_path)
    assert start_result.returncode == 0, f"start() subprocess crashed: {start_result.stderr}"
    pid = int(start_result.stdout.strip())

    try:
        became_running = _wait_until(lambda: daemon._is_process_alive(pid))
        assert became_running, "daemon never reported running within 10s"

        stop_result = _run_step("daemon.stop(timeout=10.0)", pid_path, log_path)

        assert stop_result.returncode == 0, f"stop() subprocess crashed: {stop_result.stderr}"
        assert stop_result.stdout.strip() == "True"
        assert not daemon._is_process_alive(pid)
    finally:
        if daemon._is_process_alive(pid):
            daemon._force_kill(pid)
