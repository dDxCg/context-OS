# 035 — `ctx daemon enable`/`disable`: autostart on OS login

Status: implemented

## Context

No OS-level autostart registration exists anywhere today. Flagged in
[post-release-hardening-plan.md](../agents/draft/post-release-hardening-plan.md)
Part 4. Must be an explicit opt-in CLI command, never automatic on
install/first-run - registering something to run at every OS login is a
persistent, hard-to-reverse-by-accident change (same reasoning as spec
034's PATH decision: show/act only on explicit request, never silently).

## Scope

**In**
- New `app/cli/autostart.py` module, `sys.platform`-branched like
  `daemon.py`. Content-generation is separated from OS interaction so the
  former is unit-testable without touching the real filesystem/OS state:
  - `windows_bat_content(ctx_path: Path) -> str` /
    `windows_startup_path() -> Path` (`%APPDATA%\Microsoft\Windows\Start
    Menu\Programs\Startup\chrono-ctx.bat`) - a plain `.bat` file, not a
    `.lnk` shortcut: a `.lnk` needs `pywin32` or a raw binary writer (new
    dependency, AGENTS.md §7 stop-and-ask), a `.bat` needs nothing extra
    and is just as inspectable/deletable by the user.
  - `linux_has_systemd() -> bool` (`shutil.which("systemctl")`),
    `linux_systemd_unit_content(ctx_path) -> str` /
    `linux_systemd_unit_path() -> Path`
    (`~/.config/systemd/user/chrono-ctx.service`),
    `linux_xdg_autostart_content(ctx_path) -> str` /
    `linux_xdg_autostart_path() -> Path`
    (`~/.config/autostart/chrono-ctx.desktop`) - the no-systemd fallback
    (only fires on a graphical login, not a headless server; printed
    plainly when this path is taken, not hidden).
  - `enable()`/`disable()`: orchestrate per-platform - write the file(s),
    and on Linux-with-systemd also shell out to `systemctl --user
    daemon-reload` + `enable --now`/`disable --now`. Return what was
    done (path written / command run) so the CLI layer can echo it -
    transparency matches every other decision in this draft: show the
    user the concrete file/command, never act silently.
  - macOS: out of scope (not a target platform anywhere else in this
    session's shipping plan) - `enable()`/`disable()` raise
    `NotImplementedError` with a clear message on `sys.platform ==
    "darwin"`.
- `app.py`: `ctx daemon enable` / `ctx daemon disable` subcommands, thin
  wrappers that call `autostart.enable()`/`disable()` and echo the result.

**Out**
- No macOS `launchd` support - explicitly out of scope, not silently
  broken (raises, doesn't pretend to succeed).
- No automatic enable on install/first `daemon start` - opt-in only.
- No change to `daemon start`/`stop`/`status` themselves.

## Acceptance criteria

- AC-1 (Windows). `enable()` writes a `.bat` file to the Startup folder
  that calls `<ctx_path> daemon start`; `disable()` removes it if present
  (no error if already absent).
- AC-2 (Linux, systemd present). `enable()` writes a valid systemd user
  unit whose `ExecStart`/`ExecStop` call `<ctx_path> daemon
  start`/`stop`, then runs `systemctl --user daemon-reload` and
  `systemctl --user enable --now chrono-ctx.service`; `disable()` runs
  `systemctl --user disable --now chrono-ctx.service` and removes the unit
  file.
- AC-3 (Linux, no systemd). `enable()` writes an XDG autostart `.desktop`
  file instead and reports that it only takes effect on a graphical
  login; `disable()` removes it.
- AC-4. `disable()` when nothing was ever enabled is a no-op, not an
  error.
- AC-5. `ctx daemon enable` never silently succeeds without telling the
  user exactly what file was written / command run.
- AC-6 (macOS). `enable()`/`disable()` raise `NotImplementedError`.

## Error cases

- EC-1. Given `systemctl --user enable --now` fails (e.g. no user D-Bus
  session, common in some headless/container setups), `enable()` surfaces
  the failure rather than reporting silent success - the unit file being
  written is not sufficient proof it's actually active.

## Contracts

```python
# app/cli/autostart.py
def windows_startup_path() -> Path: ...
def windows_bat_content(ctx_path: Path) -> str: ...
def linux_has_systemd() -> bool: ...
def linux_systemd_unit_path() -> Path: ...
def linux_systemd_unit_content(ctx_path: Path) -> str: ...
def linux_xdg_autostart_path() -> Path: ...
def linux_xdg_autostart_content(ctx_path: Path) -> str: ...
def enable() -> str: ...   # returns a human-readable description of what was done
def disable() -> str: ...
```

## Non-goals / open questions

- Whether Linux should ever *prefer* XDG autostart over systemd even when
  systemd is available (e.g. a user who wants graphical-login-only
  behavior) - out of scope, systemd is preferred whenever present since it
  also covers headless/server logins, which XDG autostart cannot.
- This spec's Windows/Linux paths can only be truly live-verified on their
  respective OS; this session can only live-verify the Windows path
  directly (real dev machine) - the Linux path is verified via unit tests
  with `subprocess.run`/filesystem mocked, following the same pattern
  `daemon.py`'s own tests already use for `tasklist`/`taskkill`.
