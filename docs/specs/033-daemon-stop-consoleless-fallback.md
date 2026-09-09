# 033 — `ctx daemon stop` fallback when the caller has no console (Windows)

Status: implemented

## Context

Live-reproduced twice (spec 023's own live-test, and this session's
pip-install live-test): `ctx daemon stop` from git-bash/mintty crashes with
an unhandled `OSError: [WinError 87] The parameter is incorrect` on
`os.kill(pid, signal.CTRL_BREAK_EVENT)` (`daemon.py:79`). Same command from
a real `cmd.exe`/PowerShell console works. `os.kill(..., CTRL_BREAK_EVENT)`
wraps `GenerateConsoleCtrlEvent`, which requires the *calling* process
itself to be attached to a real Win32 console - mintty/git-bash processes
are pty-based and don't reliably have one, so the call fails before it
ever reaches the target. The child daemon's own `CREATE_NEW_PROCESS_GROUP`
+ hidden-window setup (spec 023) is unaffected and still correct - this is
purely about the caller's side.

Linux is unaffected: `os.kill(pid, SIGTERM)` has no console concept at
all - no change needed there.

Today's failure mode is worse than just "stop doesn't work": it's an
unhandled exception (ugly traceback to the user) that also **leaves the
daemon running** with no automatic fallback - the user has to notice and
manually `taskkill` it.

## Scope

**In**
- `daemon.py`: `stop()` catches `OSError` around the `_send_stop_signal`
  call. On failure, falls back to writing a stop-sentinel file instead of
  immediately force-killing - preserves the graceful work-queue drain
  (specs 026/027) that a console-less caller would otherwise skip
  entirely.
- `utils/helper.py`: new `get_stop_sentinel_path()`, same
  `PROJECT_ROOT`-anchored pattern as `get_db_url()`/`get_config_path()` -
  used by both `daemon.py` (writes/cleans it up) and `local_runtime.py`
  (polls it).
- `local_runtime.py`'s existing 1s-cadence main loop (`run()`,
  `stop_event.wait(timeout=1.0)`) also checks for the sentinel file each
  iteration and treats its presence the same as the stop event being set -
  near-zero added cost, the wake-up already happens every second.
- `daemon.py`'s `start()` deletes any pre-existing sentinel file before
  spawning - a stale leftover from a previous run (e.g. the process died
  before cleanup) must not make a freshly-started daemon immediately
  self-terminate.
- `stop()` deletes the sentinel file as part of its existing cleanup
  (alongside `PID_PATH.unlink`), regardless of which mechanism (signal or
  sentinel) actually stopped the daemon - idempotent, safe even if the
  file was never created.

**Out**
- No change to the signal-successful path's behavior or timing - this is
  purely a fallback for when `_send_stop_signal` itself raises.
- No change to `_force_kill` - still the final escalation if the daemon
  hasn't exited by `timeout` regardless of which mechanism requested the
  stop.
- Linux/`SIGTERM` path - untouched, was never affected.

## Acceptance criteria

- AC-1. Given `_send_stop_signal` raises `OSError` (simulating a
  console-less caller), `stop()` does not propagate the exception - it
  falls back to writing the sentinel file and proceeds to the existing
  wait/force-kill logic.
- AC-2. Given the sentinel file is present, `local_runtime.run()`'s loop
  exits (treats it as equivalent to `stop_event` being set) within one
  poll cycle (~1s).
- AC-3. After a successful stop (either mechanism), the sentinel file no
  longer exists.
- AC-4. Given a stale sentinel file exists from a previous run, `start()`
  removes it before spawning - the new daemon does not immediately see a
  stale stop request and exit.
- AC-5. Live smoke test (not unit-mocked): from a real installed wheel, in
  a shell where `CTRL_BREAK_EVENT` fails (git-bash), `ctx daemon stop`
  completes without a traceback and the daemon process actually exits.

## Error cases

- EC-1. Given both the signal *and* writing the sentinel file fail (e.g.
  the data directory itself is unwritable), `stop()` still proceeds to its
  existing timeout/force-kill logic rather than raising - `_force_kill`
  remains the last-resort guarantee that `stop()` doesn't hang forever.

## Contracts

```python
# utils/helper.py
def get_stop_sentinel_path() -> str: ...  # PROJECT_ROOT-anchored, "data/ctx.stop"

# daemon.py
def stop(timeout: float = 10.0) -> bool: ...  # same signature, OSError now caught internally
```

## Non-goals / open questions

- Whether to *always* write the sentinel file as a belt-and-suspenders
  measure even when the signal succeeds - rejected: it would leave a
  window where a fast-restarting daemon could see a stale sentinel from
  the previous instance racing against its own startup cleanup. Signal
  stays the fast/primary path; sentinel is fallback-only.
