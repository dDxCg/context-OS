# 023 — `ctx daemon stop` cross-process signal delivery on Windows

Status: implemented

## Context

Issue #27 ([issues.md](../agents/issues.md#27-ctx-daemon-stop-crashes-on-every-call-on-windows--ctrl_break_event-to-a-detached_process-child-always-fails)):
found live via `scripts/live_integration_test.py` - `ctx daemon stop` raised
`OSError: [WinError 87] The parameter is incorrect` on every real
`ctx daemon start` (one process) then `ctx daemon stop` (a different,
later process) cycle. The existing integration test
(`test_daemon_lifecycle.py`) calls `daemon.start()` and `daemon.stop()` from
the *same* Python process, which does not reproduce this - confirmed by
direct experiment (same-process: succeeds; two separate `uv run python -c`
invocations against the same pid: fails every time).

Root cause, isolated experimentally (four creation-flag/target combinations
tested against a real Python child registering a `SIGBREAK` handler):

- `DETACHED_PROCESS` (current code): child has no console at all -
  `GenerateConsoleCtrlEvent` (what `os.kill(pid, CTRL_BREAK_EVENT)` calls
  internally on Windows) cannot target it from an unrelated process.
- `CREATE_NEW_PROCESS_GROUP` alone (no `DETACHED_PROCESS`, no
  `CREATE_NO_WINDOW`): child gets a real (visible) console of its own -
  cross-process `CTRL_BREAK_EVENT` delivery **works**, handler fires.
- `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`: **also fails** the same way
  as `DETACHED_PROCESS` - `CREATE_NO_WINDOW` also results in no console
  being allocated, not just no *visible* window.
- `CREATE_NEW_PROCESS_GROUP` + `STARTUPINFO(STARTF_USESHOWWINDOW, SW_HIDE)`
  (console allocated, window hidden after the fact rather than never
  created): cross-process `CTRL_BREAK_EVENT` delivery **works**, and no
  visible window - the combination this spec adopts.

Separately, sending the signal is not enough by itself: `vcs/runtime.py`
(spec 017) only registers a handler for `signal.SIGTERM`. On Windows,
`CTRL_BREAK_EVENT` maps to `signal.SIGBREAK`, not `SIGTERM` - confirmed by
experiment that an unhandled `SIGBREAK` does *not* raise a catchable
`KeyboardInterrupt` the way Ctrl+C does (the process is simply terminated by
the OS default action, bypassing `run()`'s `except KeyboardInterrupt` and
`self.stop()`'s cleanup entirely). Both halves are needed for a real
graceful stop on Windows.

## Scope

**In**
- `app/cli/daemon.py` `_spawn()`: Windows creation flags become
  `CREATE_NEW_PROCESS_GROUP` only (drop `DETACHED_PROCESS`), plus a
  `STARTUPINFO` with `STARTF_USESHOWWINDOW`/`wShowWindow=SW_HIDE` so no
  console window is visible despite the child now having a real console.
- `vcs/runtime.py`: on Windows, also register `signal.SIGBREAK` (in
  addition to the existing `SIGTERM` registration, which stays for
  POSIX/any future non-console signal source) against the same
  `_handle_signal` used for `SIGTERM`.
- New regression test proving the actual failure mode: two genuinely
  separate OS processes (`start()` in one, `stop()` in another), Windows
  only - the existing same-process integration test could not have caught
  this and stays as-is for the general start/status/stop contract.

**Out**
- Any change to the POSIX branch (`start_new_session=True`,
  `signal.SIGTERM`) - unaffected, this bug and fix are Windows-specific.
- Re-investigating issue #26 (daemon disappeared, no log trace) directly -
  plausibly the same root cause (a `DETACHED_PROCESS` child's relationship
  to its parent/console), but not re-verified here; #26 stays open pending
  a fresh live observation post-fix.
- `deploy/windows/register-ctx-daemon-task.ps1` - unaffected; a Scheduled
  Task's `ctx daemon start`/`stop` are exactly the cross-process pattern
  this fix targets, no template change needed.

## Acceptance criteria

- AC-1. On Windows, `daemon.start()` run in one OS process followed by
  `daemon.stop()` run in a **separate** OS process against the same PID
  file does not raise `OSError`, and the target process is no longer alive
  once `stop()` returns `True`.
- AC-2. `_spawn()`'s Windows `creationflags` no longer include
  `DETACHED_PROCESS`.

## Error cases

- None new - `stop()`'s existing timeout/force-kill escalation path is
  unchanged; this only fixes the graceful-signal step it wraps.

## Contracts

```python
# app/cli/daemon.py
def _spawn() -> int:
    """Windows: CREATE_NEW_PROCESS_GROUP + a hidden STARTUPINFO window,
    not DETACHED_PROCESS - see Context for why DETACHED_PROCESS (and
    CREATE_NO_WINDOW) break cross-process CTRL_BREAK_EVENT delivery."""

# vcs/runtime.py
class VCSRuntime:
    def run(self):
        """... registers SIGTERM always, and SIGBREAK too on win32,
        both against _handle_signal."""
```

## Non-goals / open questions

- Whether this also resolves issue #26 - plausible (same `DETACHED_PROCESS`
  choice implicated), not confirmed. Re-observe live after this ships;
  update #26 rather than closing it here.
