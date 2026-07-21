# Implementation plan — config-control CLI, audit commands, background daemon

Status: **planned, not started.** Blockers #15–#20 in [issues.md](issues.md) must be
resolved as part of this work; three of them make the feature impossible otherwise.

## Context

The CLI skeleton exists (`src/app/cli/app.py`, Typer) but only 2 of 7 commands work —
`source add` and `source remove`. The other five call `vcs/services/audit.py`, which is
100% `pass` stubs. No command prints anything, ever: the service functions return `None`
and the CLI discards it, so even the working ones are silent-success. There is no output
layer of any kind.

There is also no way to run the watcher in the background on a dev machine.
`python -m vcs.runtime` blocks the terminal, and the repo contains **zero** process
management — no PID file, no `Popen`, no signal handler, no service unit.

Decisions taken: detached process + PID file (not a service manager), audit commands in
scope, `config.yaml` write-locking deliberately out of scope.

---

## 0. Blockers — do these first

See [issues.md](issues.md) #15–#20 for the full write-ups and the evidence.

- **#15** `pyproject.toml` — add `"src/utils"` to
  `[tool.hatch.build.targets.wheel] packages`. The installed `ctx` binary cannot start
  without this.
- **#16** `src/vcs/runtime.py` — install `SIGTERM`/`SIGINT` handlers calling
  `VCSRuntime.stop()`, and make `stop()` idempotent (`LocalRuntime` already catches
  `KeyboardInterrupt` and calls its own `stop()`, so the outer handler can double-stop
  today).
- **#19** `src/vcs/shared/temp_file.py` — `TMP_DIR = Path("data/tmp")` is cwd-relative;
  route it through `anchored()` as `vcs/shared/config.py` already does.

## 1. Background daemon — `src/app/cli/daemon.py` (new)

```
ctx daemon start [--foreground]   ctx daemon status
ctx daemon stop  [--timeout N]    ctx daemon restart
ctx daemon logs  [-f] [-n N]
```

State lives beside the other runtime data, anchored via `PROJECT_ROOT`, **not** cwd:
`data/ctx.pid`, `data/ctx.log`.

**Spawn.** `subprocess.Popen([sys.executable, "-m", "vcs.runtime"])` with
`stdin=DEVNULL` and `stdout=stderr=<append-mode log file handle>`.

> **Never pass a pipe.** [live-test-report.md](live-test-report.md) Issue 5 documents this
> precisely: the runtime logs at DEBUG through `@log_enabled` on every pipeline function,
> and an undrained ~4 KB pipe buffer fills in ~30 lines, blocking every thread that logs.
> It presents as a total silent stall with no traceback and no dead thread, and already
> caused one misdiagnosis-as-deadlock in this project.

**Detach.** `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows;
`start_new_session=True` on POSIX.

**Stop — the platform split matters.** On Windows `os.kill(pid, SIGTERM)` maps to
`TerminateProcess`: abrupt, no cleanup, defeating #16 entirely. Send `CTRL_BREAK_EVENT`
instead, which raises `KeyboardInterrupt` in the child — the path the runtime *already*
handles, and the reason the new process group above is required. On POSIX send `SIGTERM`.
Then poll for exit up to `--timeout` (default 10s), escalating to `kill`/`terminate` only
if it overruns, and report that it had to.

**PID file hygiene.** Write after spawn, remove on confirmed stop. `status` must detect a
stale PID (process gone, or PID reused by something else) rather than trusting the file —
check liveness and, where cheap, that the command line still looks like our runtime.
`start` refuses if a live PID exists. `--foreground` bypasses the daemon path entirely for
debugging.

## 2. Config-control commands — `src/app/cli/app.py`

Every one is backed by an already-real function in
[configure.py](../src/vcs/services/configure.py); the work is calling and **rendering**
them.

| Command | Backing function |
|---|---|
| `ctx source add <paths…>` | `add_sources` (exists) |
| `ctx source remove <paths…>` | `remove_sources` (exists) |
| `ctx source list` | `parse_config` + `derive_watch_targets` |
| `ctx source check <path>` | `is_path_in_scope` |
| `ctx config show` | `parse_config` |
| `ctx config path` | `get_config_path` / `get_db_url` — resolved absolute paths |
| `ctx config reset` | `reset_config_file` (confirm prompt) |
| `ctx config snapshot` | `store_config_snapshot` / `recover_config` |
| `ctx health` | implement `configure.health_check()` |

Add an **output layer** (`src/app/cli/output.py`): a table renderer, `ok`/`warn`/`err`
helpers via `typer.secho`, and a `--json` global option for scripting. Do not add `rich` —
plain `typer.echo` keeps the dependency set unchanged.

`source list` should show **watch targets alongside sources**, since they are deliberately
not 1:1 — `derive_watch_targets` collapses file-granular sources to their parent
directories. That mapping is currently invisible and is the main thing a user needs to
understand about why a directory is being watched.

`ctx health` should report: config readable and parseable, DB reachable
(`DBHandler.check_connection`), schema present, snapshot present, daemon running or not,
and counts of sources / watch targets / tracked files.

Fix while here: `complete_path` (`app.py:14-20`) calls `parent.iterdir()` unguarded and
raises during shell completion of a partial path whose parent doesn't exist; and add
`no_args_is_help=True`, currently absent.

## 3. Audit commands — `src/vcs/services/audit.py`

Implement the four stubs. Each takes a `DBHandler`; reuse the existing private helpers in
[versioning.py](../src/vcs/services/versioning.py) rather than writing new SQL where
possible: `_get_context_id_by_location`, `_check_current_version`, `_get_version_hash`.

- **`get_sources(db)`** — join `locations`, with `status` (1 active / 0 out-of-scope) and a
  version count per location.
- **`get_version_list(db, path)`** — versions for the path's context, newest first, each
  with `created_at`, short hash, and **`available`** = does `BLOB_DIR/<hash>.blob` exist.
  Per issue #17 this cannot be assumed; the CLI must show it.
- **`rollback_source(db, path, version)`** — resolve hash → read blob → write to location.
  **Fail loudly when the blob is missing**, and do not touch the file. Write via
  `save_to_file(..., mode="wb")` so the watcher records the rollback as a new version —
  that is correct behaviour; a rollback is a new state, not a rewrite of history.
- **`check_diff(db, path, v1, v2)`** — two blob reads → `difflib.unified_diff` over
  `bytes_to_string()` (already UTF-8 with `errors="replace"`, so binary degrades rather
  than crashing). Default to the newest two versions when unspecified.

**Depends on issues #17 and #18 being fixed** — otherwise `rollback -v 1` can never work
for a watcher-created file, and `history` shows every version twice.

## 4. DB access from a short-lived CLI — `src/vcs/db/sqlite.py`

The CLI reads the DB *while the daemon holds it*. Today `from_url` passes no `timeout` and
never enables WAL, so a writer blocks readers and `ctx history` can hit
`database is locked` with no retry (issue #20).

- Enable `PRAGMA journal_mode=WAL` and pass a `timeout` (5–10s) in `DBHandler.from_url`.
- Add `__enter__`/`__exit__` — it already has `close`/`commit`/`rollback`/`begin`. Use it
  from every CLI command. Also fixes the runtime's connection never being closed.
- `execute()` should raise a clear error when `self.conn is None` instead of
  `AttributeError`, matching `execute_script`.

## 5. `.gitignore`

Add `data/ctx.pid` and `data/ctx.log`. `data/` is tracked-with-holes, so these would
otherwise appear as untracked noise on every `git status`.

---

## Tests

There are **no CLI tests at all** — `tests/unit/app/cli/` holds only `__init__.py` and an
orphaned `.pyc` from a file never committed, and `grep CliRunner tests/` is empty. This
work establishes the pattern.

- **`tests/unit/app/cli/test_app.py`** — `typer.testing.CliRunner` against each config
  command, using the existing `config_path` fixture (`tests/fixtures/config.py`) for
  isolation. Assert on rendered output, not just exit code — silent-success is the current
  failure mode and an exit-code-only assertion would not catch it.
- **`tests/unit/app/cli/test_daemon.py`** — PID-file lifecycle against a fake process:
  start writes a PID, `status` reports a stale PID as not-running, `stop` removes the file,
  `start` refuses when live. No real spawning here.
- **`tests/integration/app/cli/test_daemon_lifecycle.py`** — one real
  start → status → stop cycle, asserting the process actually exits and the log file is
  written. Follow the bounded-join idiom in
  `tests/integration/vcs/workers/local/test_local_runtime_shutdown.py` so a hang fails
  instead of wedging the suite.
- **`tests/unit/vcs/services/test_audit.py`** — use the `db_handler` + `seeder` fixtures.
  Cover version-list ordering; `available=False` for a hash with no blob; rollback restores
  content; **rollback refuses when the blob is missing**; diff of two versions.
- **`tests/unit/vcs/services/test_versioning.py`** — extend for #17 and #18:
  `created_handle` writes a blob; `modified_handle` does not append a duplicate-hash
  version.

> `tests/conftest.py` has a `pytest_sessionfinish` hook rewriting exit code 5 (no tests
> collected) to 0 — a leftover CI workaround from `91717e9`. If new tests fail to collect,
> **CI goes green anyway.** Verify collection counts; don't trust a green run.

## Verification

```powershell
pytest
ctx --help; ctx health; ctx source list
ctx daemon start; ctx daemon status; ctx daemon logs -n 20; ctx daemon stop
```

End-to-end:

1. `ctx daemon start` — terminal returns immediately, `data/ctx.log` grows.
2. `ctx source add <dir>` — the running daemon picks it up (rows appear). Exercises CLI and
   daemon against one `config.yaml`.
3. Edit a tracked file twice — `ctx history <file>` shows **one** new version per distinct
   content, not two (issue #18).
4. `ctx rollback <file> -v 1` on a **watcher-created** file — impossible before the #17 fix,
   must work after.
5. `ctx daemon stop` — graceful shutdown visible in the log (workers joined, no orphan
   process), PID file gone. This is what #16 buys.
6. Re-run the `live_test.py` harness — must stay 16/16, proving the signal handlers didn't
   disturb the MCP/watcher path.

## Out of scope

- **`config.yaml` write races** between CLI, MCP guardrail, and daemon (`_dump_config` is an
  unlocked truncate-and-write). Deliberate decision; tracked as a known hazard in
  `get_config_diff`'s docstring.
- **Repairing existing DB rows.** The 4 blob-less versions are unrecoverable; #17 and #18
  are forward-looking fixes only.
- **Docker image gaps** (`DATABASE_URL` unset, no volumes) from
  [live-test-report.md](live-test-report.md) — separate from a dev-machine daemon.
