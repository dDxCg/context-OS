# 017 — SIGTERM handler for `VCSRuntime`

Status: implemented

## Context

[issues.md #16](../agents/issues.md): the only stop trigger anywhere in the
codebase is `except KeyboardInterrupt` around `VCSRuntime.run()`'s call into
`LocalRuntime.run()`. There is no `signal.signal(...)` call in `src/` at
all. SIGTERM does not raise `KeyboardInterrupt` — Python's default handler
terminates the process immediately — so `docker stop`, `systemctl stop`, or
any supervisor-issued shutdown skips `VCSRuntime.stop()` entirely: the
watcher isn't stopped, worker threads aren't joined, the SQLite connection
is never closed. Latent today because the documented way to run the app is
a foreground `python -m vcs.runtime` ended with Ctrl+C; becomes live the
moment the runtime is backgrounded.

**Windows caveat (out of scope here, noted for the record):** `os.kill(pid,
SIGTERM)` on Windows maps to `TerminateProcess` — abrupt, bypasses any
handler entirely. Sending a real, catchable stop signal to a Windows child
needs `CTRL_BREAK_EVENT` against a process spawned with
`CREATE_NEW_PROCESS_GROUP`, which raises `KeyboardInterrupt` in the child —
already handled. This spec only adds the handler *inside* `VCSRuntime`;
what a future process supervisor sends is a separate concern.

## Scope

**In**
- `VCSRuntime.run()` installs a `SIGTERM` handler before entering
  `LocalRuntime.run()`. The handler sets `stop_event` so the existing
  polling loop (`LocalRuntime.run()`'s `while not stop_event.is_set():
  stop_event.wait(timeout=1.0)`) exits on its own — no new thread, no call
  into `stop()` from signal-handler context beyond a `threading.Event.set()`.
- `VCSRuntime.run()` calls `self.stop()` unconditionally after
  `local_runtime.run()` returns (today it's only called on
  `KeyboardInterrupt`, so a signal-triggered return — no exception — would
  otherwise skip cleanup).
- `VCSRuntime.stop()` becomes idempotent: a second call is a no-op, since
  it can now be reached both via the SIGTERM path and a subsequent
  `KeyboardInterrupt`.

**Out**
- Anything Windows-specific (`CTRL_BREAK_EVENT`, `CREATE_NEW_PROCESS_GROUP`)
  — that's how an external stopper targets this process, not something
  `VCSRuntime` itself does.
- `LocalRuntime.stop()`'s own idempotency — already covered by
  `test_local_runtime_shutdown.py`'s queue/bus close tests.
- A real process-supervision layer (systemd unit, Docker healthcheck) —
  separate, larger gap tracked in `ARCHITECTURE.md` §8.

## Acceptance criteria

- AC-1. `VCSRuntime.run()` registers `signal.SIGTERM` against
  `self._handle_signal` before blocking in `local_runtime.run()`.
- AC-2. Calling `self._handle_signal(signal.SIGTERM, None)` sets
  `stop_event`.
- AC-3. `VCSRuntime.stop()` called twice does not raise.

## Error cases

None — a stop is always safe to attempt; the idempotency guard is the
error-avoidance mechanism itself (AC-3).

## Contracts

```python
# vcs/runtime.py
class VCSRuntime:
    def _handle_signal(self, signum, frame) -> None: ...
    def stop(self) -> None: ...  # now idempotent
```

## Non-goals / open questions

None outstanding.
