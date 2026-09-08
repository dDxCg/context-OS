# 003 — Source path → mirror repo path mapping

## Context

`git_store` (001, 002) operates on a `(repo_path, relpath)` pair the caller
already knows. Nothing in the real system produces that pair yet —
`configure.derive_watch_targets()` returns absolute source-side directories
(the per-dir repo boundary chosen for least-privilege, per
[git-backend-plan.md](../agents/git-backend-plan.md)), and `versioning.py`'s
handlers work with absolute source paths (`event.src`/`event.dst`). This
spec is the pure mapping between the two — the last piece needed before
`versioning.py` can call `git_store` at all.

## Scope

In scope: given a set of watch-target directories (`derive_watch_targets`'s
existing output) and one absolute source path known to fall under one of
them, resolve which mirror repo owns it and what its relative path inside
that repo is. Pure function, no filesystem/git side effects, no caller
wiring into `versioning.py` yet (that's the next spec).

Out of scope: `versioning.py` handler wiring, `git_store` calls, directory
delete/move mirror-side handling (needs both this mapping *and* wiring to
matter), `GIT_REPO_DIR` config anchoring beyond declaring the constant.

## Acceptance criteria

AC-1. Given `watch_targets=["C:/src"]` and `source_path="C:/src/a.txt"`, when
`resolve_mirror_location(source_path, watch_targets)` is called, then it
returns `(GIT_REPO_DIR / "C/src", "a.txt")`.

AC-2. Given `watch_targets=["C:/src"]` and `source_path="C:/src/sub/b.txt"`,
when called, then it returns `(GIT_REPO_DIR / "C/src", "sub/b.txt")` — the
subdirectory structure under the watch target is preserved in `relpath`.

AC-3. Given `watch_targets=["C:/a", "D:/b"]` (two independent watch
directories on different drives) and `source_path="D:/b/x.txt"`, when
called, then it returns a repo path derived from `D:/b`, distinct from and
not nested under the one `C:/a` would produce.

AC-4. Given `watch_targets=["C:/src"]` and `source_path="C:/src"` (the watch
target itself — the directory-event case from
[dir-events-plan.md](../agents/dir-events-plan.md), where a delete/move
event's `src` can be the watched directory itself), when called, then it
returns `(GIT_REPO_DIR / "C/src", "")` — an empty relpath denoting the
mirror repo's own root, not an error.

## Error cases

EC-1. Given `source_path` is not under any entry in `watch_targets`, when
`resolve_mirror_location(...)` is called, it raises `PathNotWatchedError`
naming the offending path — this should be unreachable in practice (callers
only invoke it for paths the watcher already scoped in), so a clear
exception here is a defensive assertion, not expected control flow.

## Contracts

```python
# src/vcs/services/mirror_path.py

class PathNotWatchedError(ValueError):
    """source_path is not under any of the given watch_targets."""

def repo_dir_name(watch_target: str) -> str:
    """Deterministic, filesystem-safe directory name for one watch target.
    'C:/src' -> 'C/src' (drive colon stripped; path_normalize already gives
    posix separators, so no further translation is needed)."""

def resolve_mirror_location(source_path: str, watch_targets: list[str]) -> tuple[Path, str]:
    """(repo_path under GIT_REPO_DIR, relpath within that repo) for
    source_path, given the current set of watch-target directories."""
```

- `vcs/shared/config.py` gains `GIT_REPO_DIR`, anchored the same way as
  `BLOB_DIR`/`SNAPSHOT_DIR`:
  `GIT_REPO_DIR = Path(anchored(os.getenv("GIT_REPO_DIR", "data/repo")))`.
  `.gitignore` already covers `data/repo` (added in 001's Files section,
  ahead of need).
- Pure function, no I/O — `resolve_mirror_location` never touches disk or
  calls `git_store`. Both inputs (`source_path`, `watch_targets`) are
  already-normalized strings (`path_normalize`'s posix form), matching what
  `configure.derive_watch_targets()` and `event.src` already produce
  throughout the codebase — no new normalization is introduced here.

## Non-goals / open questions

- Collision between two watch targets whose `repo_dir_name` would coincide.
  Not reachable on the primary target platform (Windows drive letters are
  unique, and `path_normalize` already produces a distinct prefix per
  drive); not handled for POSIX-style paths without a drive component
  either, since this project's watch targets are always absolute paths with
  a resolvable root. Flagged, not solved, if this project ever needs to run
  its watcher against paths where that assumption doesn't hold.
- Wiring this into `versioning.py`'s four handlers — deliberately the next
  spec, not this one, to keep "one behavior per cycle."
