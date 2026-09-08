# 009 — `audit.py` read functions on the git backend

Status: implemented

## Context

`vcs/services/audit.py` is 100% `pass` stubs. [git-backend-plan.md](../agents/git-backend-plan.md)
specifies these as git-native (`get_version_list` → `git log --follow`,
`check_diff` → `git diff v1 v2`), not the older SQL/blob design in
[cli-plan.md](../agents/cli-plan.md) (superseded per
[cowork-enterprise-plan.md](../agents/cowork-enterprise-plan.md)). This spec
implements the read functions only, as prerequisite plumbing for
[audit-read-api.md](audit-read-api.md)'s HTTP layer (spec 010, next).
`rollback_source` is a write and stays out of scope here, same reasoning
audit-read-api.md already gives for excluding it from the HTTP surface.

## Scope

**In**
- `git_store.log_history(repo_path, relpath) -> list[dict]` — new primitive:
  every commit touching `relpath`, newest first.
- `git_store.diff(repo_path, relpath, rev1, rev2) -> str` — new primitive:
  unified diff of `relpath` between two revs.
- `audit.get_sources(db_handler, watch_targets=None) -> list[dict]` — every
  row in `locations`, enriched with its current git version.
- `audit.get_version_list(path, watch_targets=None) -> list[dict]` — history
  of one path.
- `audit.check_diff(path, v1, v2, watch_targets=None) -> str` — diff of one
  path between two revs.
- `audit.OutOfScopeError` — fail-closed scope gate, no elicitation (matches
  the read-only policy audit-read-api.md specifies for its future HTTP
  layer).

**Out**
- `rollback_source` (write, deferred — same reasoning as
  audit-read-api.md's exclusion of a write endpoint).
- The HTTP layer itself (spec 010).
- `CommitInfo`/`commit_info()` (spec 002) — untouched; `log_history` returns
  its own plain dicts, not `CommitInfo`, so no existing call site or test
  changes.

## Acceptance criteria

- AC-1. `get_sources(db_handler)` returns one dict per `locations` row:
  `{"location", "provider", "status", "version"}`, where `version` is that
  location's current head rev (via `current_version`, spec 008) or `None`.
- AC-2. `get_version_list(path)` on a path with N commits in its mirror
  returns N dicts, newest first, each `{"rev", "author", "timestamp",
  "message"}`.
- AC-3. `check_diff(path, v1, v2)` returns the unified diff text (as `git
  diff v1 v2 -- relpath` would print) between the two revs' content at
  `path`.

## Error cases

- EC-1. `get_version_list`/`check_diff` on a path outside the current
  config scope (`is_path_in_scope` false) raise `OutOfScopeError` before
  touching git — fail-closed, no elicitation, no partial history leaked.
- EC-2. `get_version_list`/`check_diff` on an in-scope path with no commit
  history yet (never written through `git_store`) return `[]` / `""`
  respectively, rather than raising — same "no history yet" shape as
  `current_version`'s `None` (spec 008 EC-1), not an error.

## Contracts

```python
# vcs/services/git_store.py
def log_history(repo_path: Path, relpath: str) -> list[dict]:
    """[{"rev", "author", "timestamp"}, ...] for every commit touching
    relpath, newest first. Empty list if relpath has no history."""

def diff(repo_path: Path, relpath: str, rev1: str, rev2: str) -> str:
    """Unified diff text of relpath between rev1 and rev2."""

# vcs/services/audit.py
class OutOfScopeError(PermissionError):
    """path is not in the current config source scope."""

def get_sources(db_handler: DBHandler, watch_targets: list[str] | None = None) -> list[dict]: ...
def get_version_list(path: str, watch_targets: list[str] | None = None) -> list[dict]: ...
def check_diff(path: str, v1: str, v2: str, watch_targets: list[str] | None = None) -> str: ...
```

## Non-goals / open questions

- None outstanding.
