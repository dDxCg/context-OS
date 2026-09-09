import sys

import pytest

import app.cli.autostart as autostart


@pytest.fixture(autouse=True)
def no_real_subprocess(monkeypatch):
    """Every test drives enable()/disable() through mocked OS interaction -
    none of these should ever shell out or touch real user config dirs."""
    calls = []
    monkeypatch.setattr(
        autostart.subprocess, "run",
        lambda *a, **k: calls.append((a, k)) or _FakeCompletedProcess(),
    )
    return calls


class _FakeCompletedProcess:
    returncode = 0
    stdout = ""
    stderr = ""


# --- content generation (pure, OS-independent) ---

def test_ac1_windows_bat_content_calls_daemon_start(tmp_path):
    ctx_path = tmp_path / "ctx.exe"

    content = autostart.windows_bat_content(ctx_path)

    assert str(ctx_path) in content
    assert "daemon start" in content


def test_ac2_linux_systemd_unit_content_has_start_and_stop(tmp_path):
    ctx_path = tmp_path / "ctx"

    content = autostart.linux_systemd_unit_content(ctx_path)

    assert f"ExecStart={ctx_path} daemon start" in content
    assert f"ExecStop={ctx_path} daemon stop" in content


def test_ac3_linux_xdg_autostart_content_calls_daemon_start(tmp_path):
    ctx_path = tmp_path / "ctx"

    content = autostart.linux_xdg_autostart_content(ctx_path)

    assert str(ctx_path) in content
    assert "daemon start" in content


# --- enable()/disable() orchestration ---

@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")
def test_enable_falls_back_to_sys_argv0_when_ctx_not_on_path(monkeypatch, tmp_path):
    """Regression: shutil.which("ctx") returning None must not fall back to
    sys.executable (the Python interpreter) - a bat/unit file that runs
    "python.exe daemon start" is not a valid invocation at all."""
    startup_dir = tmp_path / "Startup"
    startup_dir.mkdir()
    monkeypatch.setattr(autostart, "windows_startup_path", lambda: startup_dir / "chrono-ctx.bat")
    monkeypatch.setattr(autostart.shutil, "which", lambda name: None)
    monkeypatch.setattr(autostart.sys, "argv", [str(tmp_path / "ctx.exe")])

    autostart.enable()

    content = (startup_dir / "chrono-ctx.bat").read_text()
    assert str(tmp_path / "ctx.exe") in content
    assert "python.exe" not in content.lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")
def test_ac1_enable_writes_bat_file_on_windows(monkeypatch, tmp_path):
    startup_dir = tmp_path / "Startup"
    startup_dir.mkdir()
    monkeypatch.setattr(autostart, "windows_startup_path", lambda: startup_dir / "chrono-ctx.bat")
    monkeypatch.setattr(autostart.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(autostart.shutil, "which", lambda name: str(tmp_path / "ctx.exe"))

    autostart.enable()

    bat_path = startup_dir / "chrono-ctx.bat"
    assert bat_path.exists()
    assert "daemon start" in bat_path.read_text()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")
def test_ac1_disable_removes_bat_file_on_windows(monkeypatch, tmp_path):
    startup_dir = tmp_path / "Startup"
    startup_dir.mkdir()
    bat_path = startup_dir / "chrono-ctx.bat"
    bat_path.write_text("@echo off\n")
    monkeypatch.setattr(autostart, "windows_startup_path", lambda: bat_path)

    autostart.disable()

    assert not bat_path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")
def test_ac4_disable_is_a_noop_when_nothing_was_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "windows_startup_path", lambda: tmp_path / "chrono-ctx.bat")

    autostart.disable()  # must not raise


def test_ac2_enable_uses_systemd_when_present(monkeypatch, tmp_path, no_real_subprocess):
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    unit_path = tmp_path / "chrono-ctx.service"
    monkeypatch.setattr(autostart, "linux_systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(autostart, "linux_has_systemd", lambda: True)
    monkeypatch.setattr(autostart.shutil, "which", lambda name: "/usr/bin/ctx")

    autostart.enable()

    assert unit_path.exists()
    commands = [call[0][0] for call in no_real_subprocess]
    assert ["systemctl", "--user", "daemon-reload"] in commands
    assert ["systemctl", "--user", "enable", "--now", "chrono-ctx.service"] in commands


def test_ac3_enable_falls_back_to_xdg_autostart_without_systemd(monkeypatch, tmp_path, no_real_subprocess):
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    desktop_path = tmp_path / "chrono-ctx.desktop"
    monkeypatch.setattr(autostart, "linux_xdg_autostart_path", lambda: desktop_path)
    monkeypatch.setattr(autostart, "linux_has_systemd", lambda: False)
    monkeypatch.setattr(autostart.shutil, "which", lambda name: "/usr/bin/ctx")

    result = autostart.enable()

    assert desktop_path.exists()
    assert no_real_subprocess == []
    assert "graphical" in result.lower()


def test_ac2_disable_runs_systemctl_disable_and_removes_unit(monkeypatch, tmp_path, no_real_subprocess):
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    unit_path = tmp_path / "chrono-ctx.service"
    unit_path.write_text("[Unit]\n")
    monkeypatch.setattr(autostart, "linux_systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(autostart, "linux_has_systemd", lambda: True)

    autostart.disable()

    assert not unit_path.exists()
    commands = [call[0][0] for call in no_real_subprocess]
    assert ["systemctl", "--user", "disable", "--now", "chrono-ctx.service"] in commands


def test_ac6_enable_raises_on_macos(monkeypatch):
    monkeypatch.setattr(autostart.sys, "platform", "darwin")

    with pytest.raises(NotImplementedError):
        autostart.enable()


def test_ac6_disable_raises_on_macos(monkeypatch):
    monkeypatch.setattr(autostart.sys, "platform", "darwin")

    with pytest.raises(NotImplementedError):
        autostart.disable()


def test_ec1_enable_surfaces_systemctl_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    unit_path = tmp_path / "chrono-ctx.service"
    monkeypatch.setattr(autostart, "linux_systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(autostart, "linux_has_systemd", lambda: True)
    monkeypatch.setattr(autostart.shutil, "which", lambda name: "/usr/bin/ctx")

    class FailingCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = "Failed to connect to bus"

    monkeypatch.setattr(autostart.subprocess, "run", lambda *a, **k: FailingCompletedProcess())

    with pytest.raises(autostart.AutostartError):
        autostart.enable()
