# 022 — `init_repo()` cross-process lock

Status: implemented

## Context

Issue #25 ([issues.md](../agents/issues.md#25-init_repo-has-no-cross-process-lock--racy-git-initgit-config-on-a-live-daemon)):
found live running a real daemon against a real watch target while an MCP
client called `read_file` immediately after `create_file`. `init_repo()`
(`git_store.py:71`) is called, unlocked, from 6 call sites across
`versioning.py`/`audit.py`/`current_version()` — including both the daemon's
own commit path and any caller of `current_version()`. Two processes racing
`git init`/`git config` subprocess calls against the same already-existing
`.git` directory produced `CalledProcessError` (`git init` exit 128),
reproducible on demand at that timing.

`write()` (`git_store.py:99`) already wraps its body in `_lock_for(repo_path)`
(spec 012's cross-process `filelock.FileLock`) for exactly this class of
problem. `init_repo()` was the one caller left unguarded.

## Scope

**In**
- `git_store.init_repo()`: wrap the `git init` + two `git config` subprocess
  calls in `with _lock_for(repo_path):`, same lock instance/lock file
  `write()`/`remove()`/`move()` already use.

**Out**
- Any change to `write()`/`remove()`/`move()`/`show()` - already locked.
- A retry/backoff strategy for a still-failing `git init` - the lock
  eliminates the race, not papers over it.

## Acceptance criteria

- AC-1. `init_repo(repo_path)`, called from a second thread while another
  thread holds `_lock_for(repo_path)`, blocks until the lock is released
  instead of running concurrently.

## Error cases

- None new - `init_repo()`'s existing `GitNotAvailableError` behavior is
  unchanged; the lock only serializes concurrent callers, it doesn't change
  what a single call can raise.

## Contracts

```python
# vcs/services/git_store.py
def init_repo(repo_path: Path) -> None:
    """... now serialized per-repo via the same cross-process
    filelock.FileLock write()/remove()/move() already use."""
```

No call-site changes - every existing `init_repo(repo_path)` call keeps its
current signature and position; sequential `init_repo()` then `write()` calls
already acquire-and-release the lock separately (not nested), so this
introduces no deadlock risk.

## Non-goals / open questions

- None outstanding.
