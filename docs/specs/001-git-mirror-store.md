# 001 — Minimal git-backed write/commit primitive

## Context

[docs/agents/git-backend-plan.md](../agents/git-backend-plan.md) proposes
replacing the SQLite blob store with git. [docs/specs/conflict-ux.md](conflict-ux.md)
(optimistic-concurrency write gate) needs a `git_store` module to exist
before it can have a single failing test, and no such module exists —
`git_store.py` is 0 lines today, no `git`-related dependency is declared,
and no mirror repo exists on disk.

This spec is the smallest independently-testable slice of
`git-backend-plan.md`: init a repo, write+commit a file, read back the
commit that last touched a path. It deliberately does **not** include
per-directory mirror-path derivation, directory delete/move handling, or
migration — those are git-backend-plan.md's remaining scope, each earning
its own spec once this primitive is proven. One behavior per cycle, per
AGENTS.md §1.5.

## Scope

In scope: `init_repo`, `write` (with no-op detection on identical content),
`head_rev`, single-writer serialization for concurrent `write` calls against
the same repo path.

Out of scope: mirror-path mapping from arbitrary source paths
(`to_mirror_path`/`to_source_path`), `git rm -r`/`git mv` for directory
events, migration of existing `versions`/`BLOB_DIR` data, `git gc`
scheduling, actor-identity resolution (caller passes an already-resolved
`author` string — see [actor-attribution.md](actor-attribution.md), not yet
implemented either and not a dependency of this spec).

## Acceptance criteria

AC-1. Given a filesystem path with no git repository, when `init_repo(path)`
is called, then a valid git repository exists at that path (a `.git`
directory is present and `git -C <path> status` exits 0).

AC-2. Given an initialized repo, when `write(repo_path, "a.txt", b"hello",
message="add a.txt", author="test:1")` is called for a path with no prior
history, then `repo_path/a.txt` contains `b"hello"`, exactly one commit
exists with that message and author, and the returned rev equals
`git rev-parse HEAD`.

AC-3. Given a repo with `a.txt` already committed as `b"hello"`, when
`write(...)` is called again with byte-identical content, then no new
commit is created and the returned rev equals the rev from before the call.

AC-4. Given a repo with `a.txt` already committed as `b"hello"`, when
`write(...)` is called with `b"world"`, then exactly one new commit is
created and the returned rev differs from AC-2/AC-3's rev.

AC-5. Given a repo where `a.txt` was committed, then `b.txt` was committed
in a later commit, when `head_rev(repo_path, "a.txt")` is called, then it
returns the rev of the commit that last touched `a.txt` — not the repo's
overall `HEAD` (which now points at the `b.txt` commit).

AC-6. Given two threads call `write()` concurrently against the same
`repo_path` with different target paths, when both complete, then both
commits exist in the repo's history and neither call raised an
`index.lock`-related error.

## Error cases

EC-1. Given `write()` or `head_rev()` is called with a `repo_path` that was
never passed to `init_repo()`, the call raises `RepoNotInitializedError` —
not a raw `subprocess.CalledProcessError`.

EC-2. Given `head_rev(repo_path, relpath)` is called for a `relpath` with no
commit history yet, the call returns `None` — this is a normal state (a
brand-new path), not an error condition, and must not raise.

EC-3. Given the `git` binary is not found on `PATH`, `init_repo()` raises
`GitNotAvailableError` with a message naming what's missing — not a deep
`FileNotFoundError` traceback from inside `subprocess`.

## Contracts

```python
# src/vcs/services/git_store.py

class GitNotAvailableError(RuntimeError):
    """No `git` binary on PATH."""

class RepoNotInitializedError(RuntimeError):
    """Operation attempted on a repo_path never passed to init_repo()."""

def init_repo(repo_path: Path) -> None:
    """Create repo_path (and parents) if needed, run `git init` if not already a repo. Idempotent."""

def write(repo_path: Path, relpath: str, content: bytes, message: str, author: str) -> str:
    """Write content to repo_path/relpath, commit if it differs from what's
    currently there. Returns the resulting HEAD rev — a new one if a commit
    was made, otherwise the rev the path was already at (see AC-3)."""

def head_rev(repo_path: Path, relpath: str) -> str | None:
    """SHA of the most recent commit touching relpath, or None if relpath
    has no commit history in this repo."""
```

- No HTTP/MCP/CLI surface — pure function surface in `vcs/services/`,
  matching this project's existing layering (AGENTS.md §5).
- Side effects: filesystem writes under the caller-supplied `repo_path`
  (this spec does not own `GIT_REPO_DIR` config wiring — that's
  git-backend-plan.md's job when the mirror-path layer is built), one `git`
  subprocess invocation per operation. No DB writes, no network.
- Concurrency: an internal registry of one `threading.Lock` per `repo_path`
  (not one global lock) serializes `write()` calls against the same repo,
  so unrelated repos aren't blocked by each other once multiple repos exist
  — that parallelism isn't exercised by AC-6 (only one repo here) but the
  lock granularity is chosen now so it doesn't need revisiting later.

## Files

- [.gitignore](../../.gitignore) — **done**, added `data/repo` (the default
  `GIT_REPO_DIR`, per [git-backend-plan.md](../agents/git-backend-plan.md))
  before any `init_repo()` call runs against it. Not doing this first means
  `data/repo/.git` gets committed into *this* repo as a gitlink (mode
  `160000`) the next time someone runs `git add .` here — the outer repo
  then stores a bare commit-SHA pointer with zero actual content, and a
  later `git checkout`/`reset --hard` to a commit predating that gitlink
  deletes the entire mirror directory to match the target tree, since
  nothing was ever `.gitignore`d to keep git from managing that path's
  lifecycle. Confirmed with a throwaway repro (not committed here) before
  making this fix, not asserted from memory.
- `src/vcs/services/git_store.py` — new, this spec's Contracts section.

## Non-goals / open questions

- Mirror-path derivation, directory events, migration, GC — deferred to
  further git-backend-plan.md specs once this lands.
- Whether `write()` should also expose a `dry_run`/`would_change` check
  (useful for [conflict-ux.md](conflict-ux.md)'s optimistic-concurrency
  gate, which needs to compare an expected rev against current `head_rev`
  *before* attempting a write) — not needed by this spec's ACs, but
  `head_rev()` alone is sufficient for that caller to build the check itself
  without a new primitive, so nothing further is added here speculatively.
