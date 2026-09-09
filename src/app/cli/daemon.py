import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from utils.helper import anchored, get_stop_sentinel_path

PID_PATH = Path(anchored("data/ctx.pid"))
LOG_PATH = Path(anchored("data/ctx.log"))
STOP_SENTINEL_PATH = Path(get_stop_sentinel_path())

POLL_INTERVAL = 0.2


class DaemonAlreadyRunningError(Exception):
    def __init__(self, pid: int):
        super().__init__(f"daemon already running (pid {pid})")
        self.pid = pid


def _read_pid() -> int | None:
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text().strip())
    except ValueError:
        return None


def _is_process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _spawn() -> int:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_PATH, "ab")
    kwargs = {}
    if sys.platform == "win32":
        # No DETACHED_PROCESS/CREATE_NO_WINDOW (issue #27): either one means
        # the child gets no real console at all, and GenerateConsoleCtrlEvent
        # (what os.kill(pid, CTRL_BREAK_EVENT) calls) cannot then target it
        # from a later, unrelated process - confirmed by experiment, this is
        # exactly why every `ctx daemon stop` failed with WinError 87.
        # CREATE_NEW_PROCESS_GROUP alone gives it a real console (so
        # CTRL_BREAK_EVENT delivery works cross-process); STARTUPINFO/SW_HIDE
        # hides that console's window instead of never creating one.
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["startupinfo"] = startupinfo
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-m", "vcs.runtime"],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        **kwargs,
    )
    return proc.pid


def _send_stop_signal(pid: int) -> None:
    if sys.platform == "win32":
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    else:
        os.kill(pid, signal.SIGTERM)


def _force_kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, signal.SIGKILL)


def start() -> int:
    pid = _read_pid()
    if pid is not None and _is_process_alive(pid):
        raise DaemonAlreadyRunningError(pid)

    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    # A stale sentinel left over from a previous stop's fallback path (e.g.
    # the process died before cleanup) must not make a freshly-started
    # daemon see a stop request and exit immediately.
    STOP_SENTINEL_PATH.unlink(missing_ok=True)
    new_pid = _spawn()
    PID_PATH.write_text(str(new_pid))
    return new_pid


def status() -> dict:
    pid = _read_pid()
    if pid is not None and _is_process_alive(pid):
        return {"running": True, "pid": pid}
    return {"running": False, "pid": None}


def stop(timeout: float = 10.0) -> bool:
    pid = _read_pid()
    if pid is None or not _is_process_alive(pid):
        PID_PATH.unlink(missing_ok=True)
        STOP_SENTINEL_PATH.unlink(missing_ok=True)
        return False

    try:
        _send_stop_signal(pid)
    except OSError:
        # Caller has no console attached (e.g. mintty/git-bash on Windows) -
        # GenerateConsoleCtrlEvent needs one and fails with WinError 87
        # before it ever reaches the target process (spec 033). Fall back
        # to a sentinel file the daemon's own loop polls (~1s cadence,
        # local_runtime.py) - portable, doesn't depend on the caller's
        # console, and still lets the daemon drain gracefully (specs
        # 026/027) instead of jumping straight to a force-kill.
        STOP_SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        STOP_SENTINEL_PATH.touch()

    deadline = time.monotonic() + timeout
    while _is_process_alive(pid) and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
    if _is_process_alive(pid):
        _force_kill(pid)

    PID_PATH.unlink(missing_ok=True)
    STOP_SENTINEL_PATH.unlink(missing_ok=True)
    return True
