import os
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "chrono-ctx.service"
DESKTOP_NAME = "chrono-ctx.desktop"
BAT_NAME = "chrono-ctx.bat"


class AutostartError(Exception):
    pass


def _ctx_path() -> str:
    # shutil.which("ctx") finds the canonical entry point if PATH is already
    # correct (also works after a spec 034 PATH fix); otherwise fall back to
    # the currently-running script's own path (sys.executable is the Python
    # interpreter, not ctx - would produce a bat/unit file that tries to run
    # "python.exe daemon start", which fails).
    return shutil.which("ctx") or str(Path(sys.argv[0]).resolve())


# --- Windows ---

def windows_startup_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / BAT_NAME


def windows_bat_content(ctx_path: Path) -> str:
    return f'@echo off\r\nstart "" /B "{ctx_path}" daemon start\r\n'


def _enable_windows() -> str:
    bat_path = windows_startup_path()
    bat_path.parent.mkdir(parents=True, exist_ok=True)
    bat_path.write_text(windows_bat_content(Path(_ctx_path())))
    return f"wrote {bat_path}"


def _disable_windows() -> str:
    bat_path = windows_startup_path()
    if bat_path.exists():
        bat_path.unlink()
        return f"removed {bat_path}"
    return "nothing to disable"


# --- Linux ---

def linux_has_systemd() -> bool:
    return shutil.which("systemctl") is not None


def linux_systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def linux_systemd_unit_content(ctx_path: Path) -> str:
    return (
        "[Unit]\n"
        "Description=chrono-ctx daemon\n"
        "\n"
        "[Service]\n"
        "Type=forking\n"
        f"ExecStart={ctx_path} daemon start\n"
        f"ExecStop={ctx_path} daemon stop\n"
        "Restart=no\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def linux_xdg_autostart_path() -> Path:
    return Path.home() / ".config" / "autostart" / DESKTOP_NAME


def linux_xdg_autostart_content(ctx_path: Path) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=chrono-ctx\n"
        f"Exec={ctx_path} daemon start\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _run_systemctl(*args: str) -> None:
    result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise AutostartError(f"systemctl --user {' '.join(args)} failed: {result.stderr.strip()}")


def _enable_linux() -> str:
    ctx_path = Path(_ctx_path())
    if linux_has_systemd():
        unit_path = linux_systemd_unit_path()
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(linux_systemd_unit_content(ctx_path))
        _run_systemctl("daemon-reload")
        _run_systemctl("enable", "--now", UNIT_NAME)
        return f"wrote {unit_path} and enabled via systemctl --user"

    desktop_path = linux_xdg_autostart_path()
    desktop_path.parent.mkdir(parents=True, exist_ok=True)
    desktop_path.write_text(linux_xdg_autostart_content(ctx_path))
    return f"wrote {desktop_path} (no systemd found - only takes effect on a graphical login)"


def _disable_linux() -> str:
    unit_path = linux_systemd_unit_path()
    if linux_has_systemd() and unit_path.exists():
        _run_systemctl("disable", "--now", UNIT_NAME)
        unit_path.unlink()
        return f"disabled via systemctl --user and removed {unit_path}"

    desktop_path = linux_xdg_autostart_path()
    if desktop_path.exists():
        desktop_path.unlink()
        return f"removed {desktop_path}"

    return "nothing to disable"


# --- dispatch ---

def enable() -> str:
    if sys.platform == "win32":
        return _enable_windows()
    if sys.platform == "darwin":
        raise NotImplementedError("autostart is not supported on macOS yet")
    return _enable_linux()


def disable() -> str:
    if sys.platform == "win32":
        return _disable_windows()
    if sys.platform == "darwin":
        raise NotImplementedError("autostart is not supported on macOS yet")
    return _disable_linux()
