# 037 — bound every `git` subprocess, and stop them inheriting the MCP server's stdin

Status: implemented

## Context

From [mcp-fail-fast-and-observability-plan.md](../agents/draft/mcp-fail-fast-and-observability-plan.md)
Parts 1-2, written after spec 036 bounded the mirror-repo `FileLock` but a
live `write_file` still ran past 30s and had to be cancelled by hand.

Two independent defects, both in every
`subprocess.run(["git", ...])` call in
[git_store.py](../../src/vcs/services/git_store.py):

1. **No `timeout=` on any of the 24 calls.** A hung `git` blocks forever.
   Worse than 036's `FileLock` bug because it sits *inside* the lock:
   `write()`/`move()` run six sequential git calls under one
   `_lock_for(repo_path)`, so one stuck call holds the lock for the whole
   hang and every other process queues behind it and burns its own full
   `LOCK_TIMEOUT`. That is how a single stall compounds into a
   multi-minute cascade across concurrent MCP sessions.
2. **23 of the 24 inherit the parent's stdin** (only `_hash_object` sets a
   pipe, via `input=content`). In the MCP server that stdin *is the
   JSON-RPC pipe from the client*: if git ever prompts (credential
   helper, askpass fallback, `core.editor`) it blocks forever on a stream
   that will never carry an answer, and any bytes it consumes are stolen
   from the protocol stream - a hang plus a corrupted transport. The
   daemon is immune by construction (`_spawn()` already passes
   `stdin=subprocess.DEVNULL`); the MCP server never got the same
   treatment.

Both fixes touch the same 24 call sites, so they ship together rather
than editing every one twice.

## Scope

**In**
- `git_store.py`: a module-level `GIT_TIMEOUT = 4.0`, passed as
  `timeout=` to every `subprocess.run(["git", ...])` call.
- `git_store.py`: `stdin=subprocess.DEVNULL` on every git call that does
  not already pass `input=`, plus `GIT_TERMINAL_PROMPT=0` in the
  subprocess environment so git fails instead of attempting to prompt at
  all.
- A single private helper (`_run_git(...)`) that every existing call site
  routes through, so the timeout/stdin/env policy is applied in one place
  and a future call site can't silently opt out of it by being written
  the old way.

**Out**
- Changing `LOCK_TIMEOUT` - stays 30s. The budget in the draft was chosen
  so 036's shipped value needs no revision (6 × 4s = 24s worst-case hold
  against a 30s wait, a deliberate 6s margin so a waiter always outlasts
  a holder that is genuinely working).
- Retry/backoff on timeout - a timeout means genuinely stuck, and
  retrying inside the lock only extends the hold. Out, same reasoning as
  spec 022's "the lock eliminates the race, it doesn't paper over a
  still-failing op".
- MCP-layer logging and the other bounded-wait fixes (draft Parts 3-6) -
  spec 038.

## Acceptance criteria

- AC-1. Every `subprocess.run` in `git_store.py` goes through the shared
  helper - no direct `subprocess.run(["git", ...])` call sites remain.
- AC-2. The helper passes `timeout=GIT_TIMEOUT` (4.0), so a git command
  that hangs raises `subprocess.TimeoutExpired` instead of blocking
  forever - verified with a fake slow `git` and a short overridden
  timeout, so the test stays fast.
- AC-3. The helper passes `stdin=subprocess.DEVNULL` for calls that don't
  supply `input=`, and does *not* override stdin for `_hash_object`,
  which needs its pipe to receive content.
- AC-4. The helper sets `GIT_TERMINAL_PROMPT=0` in the child environment,
  preserving the rest of `os.environ` (and, for `_commit_tree`, the
  `GIT_AUTHOR_*`/`GIT_COMMITTER_*` values `_commit_env()` already
  supplies).
- AC-5. Existing behavior is unchanged for every normal path -
  `check=True` still raises `CalledProcessError`, `capture_output`/`text`
  still apply per call site, and the full existing `test_git_store.py`
  suite passes untouched.
- AC-6. Worst-case lock hold stays under `LOCK_TIMEOUT`: 6 × `GIT_TIMEOUT`
  ≤ `LOCK_TIMEOUT`, asserted directly so the two constants can't drift
  apart into the incoherent state the draft describes.

## Error cases

- EC-1. A `subprocess.TimeoutExpired` raised mid-`write()` (between
  `update-index` and `commit-tree`) leaves a staged-but-uncommitted
  index - exactly the state spec 027's `reset_stale_index()` already
  clears at boot. Not a new failure *kind*, only newly reachable without
  a force-kill; no new recovery machinery needed.
- EC-2. `TimeoutExpired` is **not** an `OSError` subclass (unlike
  `filelock.Timeout`), so it is *not* covered by
  `app/mcp/server.py`'s existing `IO_ERRORS` catch. Handling it at the
  MCP boundary belongs to spec 038; this spec only guarantees the call
  gives up.

## Contracts

```python
# vcs/services/git_store.py
GIT_TIMEOUT: float = 4.0

def _run_git(args: list[str], repo_path: Path, *, check: bool = True,
             text: bool = False, input: bytes | None = None,
             env: dict | None = None) -> subprocess.CompletedProcess: ...
```

## Non-goals / open questions

- ~~Whether 4s holds on a cold or AV-scanned Windows box~~ - **closed by
  live measurement** on this Windows box: `init_repo()` cold (3 calls)
  0.075s, `write()` (6 calls under one lock) 0.135s, `head_rev()` 0.024s
  - roughly 25ms per git call, ~160x headroom against the 4s bound. The
  budget is generous, not tight.
