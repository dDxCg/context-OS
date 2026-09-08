# 012 — `rollback_source` + cross-process repo lock

Status: implemented

## Context

`audit.rollback_source` is the last stub in `audit.py`. Per
[git-backend-plan.md](../agents/git-backend-plan.md): "route rollback
through the daemon rather than having the CLI touch the mirror repo
directly" was the original caution — but that assumed a control-channel
that doesn't exist. The real risk it's guarding against is narrower:
`git_store.py`'s per-repo lock is a `threading.Lock`, in-process only. The
CLI and HTTP API are separate OS processes from the daemon; two of them
racing real `git` subprocess calls against the same mirror repo (not just a
lost-update on content, but genuinely interleaved `git add`/`git commit`
invocations) can corrupt the repo. A `threading.Lock` cannot see across
that process boundary.

Decision (asked and confirmed): swap the per-repo lock for a real
cross-process file lock (`filelock`, new dependency), rather than building
a daemon control channel. Every git-mutating operation already funnels
through `_lock_for(repo_path)` (specs 001/002/005), so this is a
lock-implementation swap, not a call-site change.

This also finally gives `write_with_check`/`ConcurrentEditError` (spec 002)
a real caller — built then, never wired into any actual write path.
`rollback_source` is a natural fit: rolling back to `rev` is itself a
"write, but only if nobody committed something newer since I looked" —
exactly what `write_with_check(expected_rev=...)` already does.

## Scope

**In**
- `git_store.py`: `_lock_for` returns a `filelock.FileLock` (one lock file
  per repo, e.g. `repo_path/.chrono-ctx.lock`) instead of a
  `threading.Lock`. Safe within a process too — `filelock.FileLock` is
  documented thread-safe when the same instance is reused, same as today's
  module-level dict-of-locks pattern.
- `git_store.show(repo_path, relpath, rev) -> bytes` — new primitive,
  `git show {rev}:{relpath}`.
- `audit.rollback_source(path, rev, watch_targets=None) -> str` — reads
  `rev`'s content via `show`, writes it back via `write_with_check`
  (`expected_rev` = the path's current head, so a rollback racing a
  concurrent newer commit raises `ConcurrentEditError` instead of silently
  clobbering it), then writes the same content to the real source path so
  the file on disk matches. Fail-closed via `OutOfScopeError`, same as
  `get_version_list`/`check_diff`.
- `ctx rollback <path> --version/-v REV` wired for real; prints the new
  rev on success.

**Out**
- A daemon control channel / IPC (git-backend-plan.md's original framing) -
  the file lock covers the actual corruption risk without it.
- Actor attribution for the rollback commit beyond a fixed `cli:rollback`
  label - real per-caller actor capture is issue #23's scope, not this
  spec's.
- HTTP endpoint for rollback - audit-read-api's original scoping (no write
  endpoint) still stands; this is CLI-only.

## Acceptance criteria

- AC-1. `rollback_source(path, rev)` restores `path`'s mirror content to
  what it was at `rev`, as a new commit (history stays append-only, no
  rewrite).
- AC-2. After `rollback_source`, the real file at `path` on disk matches
  `rev`'s content byte-for-byte.
- AC-3. `rollback_source` returns the new commit's rev, which differs from
  both `rev` (the restored content's original commit) and the pre-rollback
  head (unless they happen to already match byte-for-byte, in which case
  `write`'s existing no-op detection applies, same as any other write).
- AC-4. `ctx rollback <path> -v <rev>` performs the rollback and prints the
  new rev.

## Error cases

- EC-1. `rollback_source` on an out-of-scope path raises `OutOfScopeError`
  before touching git.
- EC-2. Two processes racing a git-mutating call against the same repo
  (e.g. the daemon committing while a CLI rollback is in flight) no longer
  interleave `git` subprocess invocations - the second blocks on the file
  lock until the first releases it.

## Contracts

```python
# vcs/services/git_store.py
def show(repo_path: Path, relpath: str, rev: str) -> bytes:
    """Content of relpath as of rev."""

# _lock_for(repo_path) -> filelock.FileLock, same call sites as today
# (write/remove/move all already do `with _lock_for(repo_path):`)

# vcs/services/audit.py
def rollback_source(path: str, rev: str, watch_targets: list[str] | None = None) -> str: ...
```

## Non-goals / open questions

- None outstanding.
