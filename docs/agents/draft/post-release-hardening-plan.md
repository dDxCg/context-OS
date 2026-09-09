# Draft — post-0.1.1 hardening: daemon stop, PATH, --version, autostart

Status: draft, not started. Found live-testing the real `pip install
chrono-ctx` package (0.1.1) after specs 029-031 shipped. Four independent
items, each small enough to be its own spec later.

## Part 1 — `ctx daemon stop` WinError 87 (Windows-only)

**Repro**: `ctx daemon stop` from git-bash/mintty →
`OSError: [WinError 87] The parameter is incorrect` on
`os.kill(pid, signal.CTRL_BREAK_EVENT)` (`daemon.py:79`). Same command from
a real `cmd.exe`/PowerShell console works. Confirmed twice now (spec 023
live-test, and this session's pip-install live-test) - reproducible, not a
fluke.

**Root cause (likely)**: `os.kill(..., CTRL_BREAK_EVENT)` wraps
`GenerateConsoleCtrlEvent`, which requires the *calling* process itself to
be attached to a real Win32 console. mintty/git-bash processes don't
necessarily have one (they're pty-based) - so the call fails before it ever
reaches the target. This is about the caller's console, not the child's
(the child's `CREATE_NEW_PROCESS_GROUP` + hidden-window setup from spec 023
is unrelated and still correct).

**Not a Linux problem**: POSIX `os.kill(pid, SIGTERM)` has no console
concept at all - the existing Linux path is already correct and portable.
"Setup for Linux" isn't needed for stop specifically; see Part 4 for where
Linux *does* need its own path (autostart).

**Fix direction**: don't let a console-less caller turn into an unhandled
traceback + a leaked process. In `_send_stop_signal`/`stop()`
(`daemon.py`), catch `OSError` around the `CTRL_BREAK_EVENT` call and fall
back immediately to `_force_kill()` (already exists, uses `taskkill /F`,
doesn't depend on the caller having a console) rather than propagating the
exception. Tradeoff: loses the graceful SIGTERM-equivalent drain (spec
026/027's work-queue drain) for this one fallback path specifically - worth
flagging to the user as a real, honest regression risk for the mintty case,
not hidden. A better fix (no data-loss regression) is a console-independent
stop signal - e.g. the daemon polls a `data/ctx.stop` sentinel file on its
existing loop cadence in addition to handling SIGTERM/SIGBREAK, and
`ctx daemon stop` writes that file first, only escalating to
`CTRL_BREAK_EVENT`/`_force_kill` if the daemon hasn't exited by the
timeout. Needs to check the actual watcher/worker loop's poll interval
(`local_runtime.py`/`bus.py`) before committing to this - if the loop
already wakes every second or so for the watchdog check, this is nearly
free; if it blocks indefinitely on an OS-level file-watch event, this needs
its own polling thread, more cost.

## Part 2 — `ctx` not on PATH after `pip install --user` (cross-platform)

**Repro**: pasted install output this session -
`WARNING: The script ctx.exe is installed in
'C:\Users\admin\AppData\Roaming\Python\Python314\Scripts' which is not on
PATH.` Same class of problem exists on Linux (`~/.local/bin` often missing
from `PATH` on a fresh user-install) - pip prints an equivalent warning
there too.

**Fix direction, mirroring the git-preflight precedent already in this
draft's Part 5** (detect + prompt, never silently mutate shared state):
`ctx` can't fix its own invisibility on its *first* run (if `ctx` isn't on
PATH, the user can't run `ctx` to find that out) - so this has to be
something pip itself prints, or a one-time nudge from whatever *does*
successfully invoke `ctx` first (e.g. `ctx daemon start`'s existing
first-run config prompt from Part 1 of the shipping draft could append a
PATH check). Concretely: at that first-run prompt, check whether
`sys.argv[0]`'s directory is on `os.environ["PATH"]`; if not, print the
platform-specific fix (`setx PATH "%PATH%;<dir>"` on Windows needs a new
shell to take effect; `export PATH="$PATH:<dir>"` / append to
`~/.bashrc`/`~/.zshrc` on Linux/macOS) rather than auto-editing `PATH` or a
shell rc file - same reasoning as the git-install decision: modifying a
user's persistent shell/registry config is a "hard to reverse, affects
things outside this project" action, and should be shown, not done, unless
explicitly opted into (e.g. behind an explicit `--fix-path` flag on that
prompt, not the default).

## Part 3 — `ctx --version`

No `--version` flag or root callback exists today (`app.py`'s `cli` Typer
instance has no callback at all). Standard Typer pattern: add a
`@cli.callback()` with an `Annotated[bool, typer.Option("--version",
callback=..., is_eager=True)]` parameter. Version string via
`importlib.metadata.version("chrono-ctx")` (stdlib, no new dependency) -
reads the installed package's metadata directly, so it's automatically
correct for a real `pip install` and doesn't need to duplicate
`pyproject.toml`'s `version` field as a second source of truth. Needs a
fallback for the *editable/source-checkout* case where
`importlib.metadata` may not find a matching distribution depending on how
`uv sync` registered it - confirm empirically (live-test both a source
checkout and a real wheel install) rather than assuming.

## Part 4 — autostart-on-boot config via CLI

Not implemented anywhere today - no OS registration exists. Needs an
explicit opt-in subcommand (e.g. `ctx daemon enable`/`ctx daemon disable`),
never automatic on install/first-run - registering something to run at
every OS login is a persistent, hard-to-reverse-by-accident change that
belongs behind an explicit user action, same reasoning as Part 2.

Per-OS mechanism (needs its own small module, `sys.platform`-branched like
the rest of `daemon.py`):

- **Windows**: either (a) a shortcut to `ctx.exe daemon start` dropped in
  the user's Startup folder
  (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`), or (b) a
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` registry value.
  Shortcut approach is more transparent/inspectable (user can just look in
  the folder, delete the .lnk to undo) - prefer it over a hidden registry
  edit, in the spirit of "don't act invisibly." Needs `pywin32` or a raw
  `.lnk` writer to create the shortcut file, or a `.bat`/`.cmd` wrapper
  dropped in Startup instead of a real `.lnk` (simpler, no new
  dependency - just a batch file calling `ctx.exe daemon start`) - **flag
  before commit**: this needs the AGENTS.md §7 stop-and-ask if a real `.lnk`
  requires a new dependency; the `.bat`-file route avoids that entirely and
  is likely preferable for that reason alone.
- **Linux**: a `systemd --user` unit
  (`~/.config/systemd/user/chrono-ctx.service`) +
  `systemctl --user enable --now chrono-ctx`, for systems with systemd
  (the common case). Needs a non-systemd fallback story (XDG autostart
  `~/.config/autostart/chrono-ctx.desktop`, which only fires on a graphical
  login, not a headless server) - open question which one `ctx daemon
  enable` should default to, or whether it should detect which is
  available and pick one, printing what it did either way (again: show the
  concrete unit/file content, same transparency principle as everywhere
  else in this draft).
- **macOS**: out of scope for this draft (not a target platform mentioned
  anywhere else in the shipping plan so far) - a `launchd` plist would be
  the equivalent if this ever gets picked up.

## Suggested spec order

These are independent - no ordering dependency between them. Cheapest/most
isolated first if picked up in one pass: **Part 3 (`--version`)** → **Part
1 (daemon stop fix)** → **Part 2 (PATH detection)** → **Part 4 (autostart)**,
roughly increasing in surface area and OS-specific branching to get right.
