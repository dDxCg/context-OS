# 027 — reset a stale mirror index left by a force-killed write

Status: implemented

## Context

[draft/shutdown-and-force-kill-recovery-plan.md](../agents/draft/shutdown-and-force-kill-recovery-plan.md),
approved. Graceful `SIGTERM`/`ctx daemon stop` already drains every queued
event before exiting (confirmed by tracing `local_runtime.py`'s
`stop()`/`consumer_worker.py`'s drain loop - no code change needed there).
A force-kill (`SIGKILL`, `taskkill /F`, an external `kill -9`) is different:
`write()`/`remove()`/`move()` (`git_store.py`, spec 025) each run several
separate `git` subprocess calls under one lock - stage into the index
(`update-index`/`git rm --cached`), then build and commit a tree
(`write-tree`/`commit-tree`/`update-ref`). A kill landing between those two
halves leaves the index holding a staged-but-never-committed change while
`HEAD` stays unchanged. `write()` always builds its tree from the *entire*
current index, not just the path it's touching - so the next legitimate
write to any path in that same mirror repo silently mixes the crashed
write's stale staged content into an otherwise normal-looking commit. No
error, no warning, wrong tree. Pre-existing risk (the old porcelain `git
add`+`git commit` had the identical two-step gap) - no self-heal for it
exists anywhere in `git_store.py` today.

## Scope

**In**
- `git_store.py`: a function that resets one repo's index to exactly match
  `HEAD`'s tree (`git read-tree`), or to empty if `HEAD` is unborn
  (`git read-tree --empty`) - either way discarding anything staged that
  isn't part of an actual commit. Safe unconditionally, not a heuristic:
  every real write holds `_lock_for(repo_path)` for its entire
  stage-through-commit sequence, so if the lock is acquirable, no write is
  legitimately in progress, and any index/HEAD mismatch can only be crash
  leftover.
- `git_store.py` or `mirror_path.py`: enumerate every existing mirror repo
  under a base directory. Mirror repos nest at
  `repo_dir_name(watch_target)`-derived depth under `GIT_REPO_DIR`
  (colon stripped, slashes kept - see `mirror_path.repo_dir_name`), not
  flat immediate children, so this walks recursively and must not descend
  into a found repo's own internals (`.git/objects`, or `objects/` for a
  bare repo) once it identifies the repo root.
- `Initializer.init()`: call the reset for every existing mirror repo,
  once, early - before any of this boot's own writes (spec 026's
  reconcile, the backfill scan) touch those repos.

**Out**
- Any change to `write()`/`remove()`/`move()`'s own subprocess sequence -
  making that sequence atomic (e.g. via a single `git commit-tree`-only
  path with no separate staging step) is a different, larger redesign, not
  attempted here. This spec repairs the aftermath, not the race itself.
- Any change to the graceful-shutdown path (`local_runtime.py`,
  `consumer_worker.py`, `bus.py`) - already correct, confirmed by tracing,
  not touched.
- Logging/counting how many repos needed a reset - an observability
  nice-to-have per the draft's open questions, not required for
  correctness, not built here.
- Any other force-kill damage surface - SQLite (crash-safe by its own
  journal/WAL), the `filelock` cross-process lock (kernel-released on
  process death), `data/ctx.pid` (already handled by `daemon.py start()`'s
  liveness check), `pending_actor_hints` (already TTL'd) - all already
  safe, confirmed in the draft, not re-litigated here.

## Acceptance criteria

- AC-1. Given a repo with committed history whose index has a
  staged-but-uncommitted entry left by a simulated interrupted write (the
  index was updated via `update-index`/`git rm --cached` but no
  `commit-tree`/`update-ref` ever followed), when the reset runs, then the
  index matches `HEAD`'s tree exactly - a subsequent `write()` to an
  unrelated path in the same repo produces a tree containing only `HEAD`'s
  existing paths plus that new write, never the stale staged content.
- AC-2. Given a repo whose index already matches `HEAD` (no crash
  happened, or a prior reset already ran), the reset is a no-op - `HEAD`
  and the index are unchanged, no commit is created.
- AC-3. Given a repo with **unborn `HEAD`** (zero commits ever) and a
  staged-but-uncommitted entry (a crash before that repo's very first
  commit ever completed), the reset discards it too - the next `write()`
  to that repo produces a tree containing only what that write itself
  adds, not the stale entry.
- AC-4. The repo-enumeration helper finds a mirror repo nested several
  directories deep under the base directory (matching real
  `repo_dir_name()` output, e.g. `base/C/Users/x/project`), not just
  repos that are immediate children of the base directory.
- AC-5. The repo-enumeration helper does not return paths inside a found
  repo's own git internals (e.g. nothing under `<repo>/objects/` or
  `<repo>/.git/` is itself returned as a separate "repo").
- AC-6. `Initializer.init()` resets every existing mirror repo before any
  write happens this boot - given a repo with a stale staged entry and a
  `config.yaml` that causes a real write to a *different* path in that
  same repo during this boot's backfill scan, the resulting commit's tree
  does not contain the stale entry.

## Error cases

- EC-1. Given the base directory under which mirror repos would live
  doesn't exist yet (fresh install, nothing has ever been mirrored), the
  enumeration helper returns an empty list, not an error - `Initializer.init()`
  must not fail on a brand-new install with no `GIT_REPO_DIR` on disk yet.

## Contracts

```python
# vcs/services/git_store.py — new
def reset_stale_index(repo_path: Path) -> None: ...
def existing_mirror_repos(base_dir: Path) -> list[Path]: ...
def reset_stale_indexes(base_dir: Path) -> list[Path]: ...  # calls reset_stale_index() for each, returns what it touched

# vcs/initialize.py — Initializer.init(), same public shape, one more step
def init(self) -> None: ...
```

## Non-goals / open questions

- Whether `existing_mirror_repos`/`reset_stale_indexes` belong in
  `git_store.py` (co-located with the write functions whose failure mode
  they repair - chosen) or `mirror_path.py` (co-located with
  `repo_dir_name`/`repo_path_for`, which already know the directory-nesting
  shape) - placed in `git_store.py` since the enumeration's own filter
  (`_is_initialized`) already lives there, and `mirror_path.py` has no
  existing dependency on `subprocess`/git internals to date.
- Observability (logging a count of repos actually reset) - deferred per
  the draft, not required for correctness.
