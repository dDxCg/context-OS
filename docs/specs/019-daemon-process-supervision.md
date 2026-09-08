# 019 — `ctx daemon` process supervision for the VCS runtime

Status: implemented

## Context

`ARCHITECTURE.md` §8 / Tier 3: "No process supervision across the three
entrypoints — running the full system today means starting the daemon, the
MCP server, and the HTTP API by hand." `python -m vcs.runtime` blocks the
foreground terminal and the repo has zero process management for it — no
PID file, no `Popen`, no way to background it on a dev machine.

This closes that gap for the **watch daemon specifically** — the one
entrypoint that actually needs backgrounding for a normal dev workflow (the
MCP server is spawned per-need by an MCP client via `fastmcp.json`, not
something this project backgrounds itself; the HTTP API is a request-driven
server most operators already run via `uvicorn`/a reverse proxy the same
way as any other web service). Extending the same `start`/`stop`/`status`
shape to the HTTP API is a natural follow-on, not built here — noted as a
deliberate scope decision, not an oversight.

Design carried over from `docs/agents/cli-plan.md` §1 (written earlier,
never implemented): **detached process + PID file**, not a service manager
(systemd unit / Docker healthcheck) — this project runs on a bare dev
machine, adding a service-manager dependency for that is over-engineering
for the actual need. That plan's blockers (issues #15/#16/#19/#20 — wheel
packaging, `SIGTERM` handling, `TMP_DIR` anchoring, SQLite WAL/timeout) are
already closed by specs 014-017, so this spec is scoped to just the daemon
lifecycle itself; cli-plan.md's other sections (config-control CLI, audit
commands, DB access) are likewise already done via specs 009/011/012/016
under different names.

## Scope

**In**
- `app/cli/daemon.py` (new): `start()`, `stop(timeout=10.0)`, `status()`,
  PID-file lifecycle (`data/ctx.pid`, anchored via `anchored()` — not cwd,
  same reasoning as `TMP_DIR`/spec 015), log file (`data/ctx.log`,
  append-mode, never a pipe — an undrained pipe on this project's
  `@log_enabled` DEBUG-level logging fills and stalls every thread that
  logs, a documented past incident).
- Spawn: `subprocess.Popen([sys.executable, "-m", "vcs.runtime"], ...)`,
  detached (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows,
  `start_new_session=True` on POSIX), `stdin=DEVNULL`,
  `stdout=stderr=<the log file>`.
- Stop: POSIX sends `SIGTERM` (spec 017's handler already does a clean
  shutdown); Windows sends `CTRL_BREAK_EVENT` against the process group
  (raises `KeyboardInterrupt` in the child — the path `VCSRuntime.run()`
  already handles), since `os.kill(pid, SIGTERM)` on Windows maps to
  `TerminateProcess` and would defeat spec 017 entirely. Polls for exit up
  to `timeout` seconds, escalates to a hard kill (`SIGKILL` /
  `taskkill /F`) only past that, and reports whether it had to escalate.
- `status()` never trusts the PID file blindly — a stale PID (process gone,
  or reused by something else) reads as not-running.
- `app/cli/app.py`: `ctx daemon start|stop|status` wired to the above.
- `.gitignore`: add `data/ctx.pid`, `data/ctx.log` (`data/` is
  tracked-with-holes; these would otherwise show as untracked noise on
  every `git status`).

**Out**
- `ctx daemon restart` and `ctx daemon logs [-f]` — nice-to-haves from the
  original plan, not core supervision; `status`/`start`/`stop` is the
  complete lifecycle this spec commits to.
- `--foreground` flag — `python -m vcs.runtime` already is the foreground
  path; nothing new to add.
- Backgrounding the MCP server or HTTP API — see Context.
- `config.yaml` write-locking between CLI/MCP/daemon — pre-existing,
  documented, deliberately out of scope hazard (`get_config_diff`'s
  docstring), unrelated to this spec.

## Acceptance criteria

- AC-1. `start()` with no live daemon spawns a detached process, writes its
  PID to the PID file, and returns immediately (does not block on the
  child).
- AC-2. `start()` when the PID file names a still-live process raises
  `DaemonAlreadyRunningError` and does not spawn a second process.
- AC-3. `start()` when the PID file is stale (named process no longer
  alive) proceeds to spawn, exactly as AC-1.
- AC-4. `status()` reports `running=True` with the PID when the PID file
  names a live process.
- AC-5. `status()` reports `running=False` when there is no PID file, or
  the PID file is stale.
- AC-6. `stop()` on a running daemon sends the platform-appropriate stop
  signal, waits for exit, and removes the PID file once the process is
  confirmed gone.
- AC-7. `stop()` when already stopped (no PID file, or stale) is a no-op —
  does not raise, does not send a signal to a possibly-reused PID.

## Error cases

- EC-1. `stop()` against a process that does not exit within `timeout`
  escalates to a hard kill and still removes the PID file afterward —
  never hangs indefinitely waiting for a wedged process.

## Contracts

```python
# app/cli/daemon.py
class DaemonAlreadyRunningError(Exception): ...

def start() -> int: ...                    # returns the new PID
def stop(timeout: float = 10.0) -> bool: ... # True if it stopped something
def status() -> dict: ...                   # {"running": bool, "pid": int | None}
```

```
ctx daemon start   # exit 1 + message if already running
ctx daemon stop    # "daemon not running" if nothing to stop
ctx daemon status  # "running, pid N" / "stopped"
```

## Non-goals / open questions

None outstanding.
