# 036 — bounded timeout on the cross-process mirror-repo lock, fail fast through MCP

Status: implemented

## Context

Issue [#29](../agents/issues.md#29-_lock_for-s-cross-process-filelock-has-no-timeout--a-contended-write-hangs-forever-not-until-any-bounded-30s):
`git_store.py`'s `_lock_for()` constructs `FileLock(str(repo_path /
".chrono-ctx.lock"))` with no `timeout=` - `filelock.FileLock`'s default is
`-1` (block indefinitely, confirmed via `inspect.signature`). Every
`write()`/`remove()`/`move()`/`init_repo()`/`reset_stale_index()` call
acquires this lock, so any of them can stall forever under contention.
Live-observed running the MCP server from 3 concurrent Claude Code sessions
against the same project/mirror repos: an MCP `write_file` call hung, then
surfaced a confusing transport-level error once the *client's* own
unrelated timeout gave up - chrono-ctx itself never decided to fail fast.

Decided: cap the lock wait at 30s (matching `DBHandler.from_url()`'s
existing `timeout=30.0` convention, issue #20/spec 016 - same "how long is
reasonable to wait for contention before giving up" judgment call, already
made once in this codebase), and make the MCP tool-call boundary surface a
clean, structured error instead of hanging or leaking a raw stack
trace/transport timeout.

`filelock.Timeout` is a subclass of `OSError` (confirmed:
`Timeout.__mro__` includes `OSError`) - already covered by
`app/mcp/server.py`'s existing `IO_ERRORS = (OSError, UnicodeError,
shutil.Error)` catch-all *wherever that catch already wraps the call*. The
actual gap isn't a missing exception type, it's that two `current_version()`
call sites in `server.py` aren't wrapped in `try/except IO_ERRORS` at all
today: `read_file`'s version lookup (line 82) and
`_check_expected_version()` (used by `write_file`/`delete_file`, called
before their own `try` blocks even start).

## Scope

**In**
- `git_store.py`: module-level `LOCK_TIMEOUT = 30.0`; `_lock_for()` passes
  `timeout=LOCK_TIMEOUT` to `FileLock(...)` - single change point, applies
  uniformly to every existing caller (write/remove/move/init_repo/
  reset_stale_index), both the synchronous MCP-tool-call path and the
  daemon's own async watcher-triggered commits.
- `app/mcp/server.py`: wrap the two currently-unprotected `current_version()`
  calls (`read_file`'s version lookup, `_check_expected_version()`) in the
  same `except IO_ERRORS` pattern already used everywhere else in this
  file - `filelock.Timeout` needs no special-casing, it's already an
  `OSError` subclass.

**Out**
- The daemon-side async path (`local_consumer.py`'s worker thread) already
  has its own top-level `except Exception: ... continuing` boundary
  (`consumer_worker.py:57`) - a `filelock.Timeout` raised there is already
  logged and swallowed without crashing the worker, no change needed for
  that side to be safe; this spec's MCP-boundary change is about giving
  the *caller* a fast, clear answer, which only applies to the synchronous
  MCP path.
- Making the 30s bound configurable (env var, parameter) - matches
  `DBHandler`'s existing hardcoded-default convention, no config surface
  exists for that timeout either; speculative for a single constant.
- Any retry/backoff on timeout - out of scope, matches spec 022's original
  "the lock eliminates the race, it doesn't paper over a still-failing
  op" stance.
- The unconfirmed, not-reproduced-twice `git config user.email` exit-1
  shape noted in issue #29 - flagged there as possibly unrelated (Windows
  AV/OneDrive interference), not addressed here.

## Acceptance criteria

- AC-1. `_lock_for(repo_path)` returns a `FileLock` whose `.timeout == 30.0`.
- AC-2. Given the lock for a repo is already held, a second call through
  any of `write()`/`remove()`/`move()`/`init_repo()`/`reset_stale_index()`
  raises `filelock.Timeout` once the wait exceeds the configured timeout,
  rather than blocking forever - verified with a short overridden timeout
  (not a real 30s wait) so the test stays fast.
- AC-3. An MCP `read_file` call whose `current_version()` lookup raises
  `filelock.Timeout` returns `{"status": "error", "reason": ...}` instead
  of hanging or propagating an unhandled exception through the MCP
  transport.
- AC-4. An MCP `write_file`/`delete_file` call whose
  `_check_expected_version()` raises `filelock.Timeout` returns the same
  structured error shape, before ever touching the filesystem.

## Error cases

- EC-1. The daemon's own async commit path hitting the same `Timeout` after
  30s is unaffected in shape (already caught-and-logged by
  `consumer_worker.py`'s existing handler-failure boundary) - not a new
  failure mode, just now bounded instead of unbounded.

## Contracts

```python
# vcs/services/git_store.py
LOCK_TIMEOUT: float = 30.0
def _lock_for(repo_path: Path) -> FileLock: ...  # same signature, now timeout-bounded

# app/mcp/server.py
# read_file / _check_expected_version: current_version() calls now inside
# the existing except IO_ERRORS boundary - no signature change.
```

## Non-goals / open questions

- Whether 30s is the right number for an interactive agent waiting on a
  tool call (vs. the daemon's own background retry tolerance) - reused
  the existing `DBHandler` convention rather than picking a new number;
  revisit if 30s proves too long/short in practice.
