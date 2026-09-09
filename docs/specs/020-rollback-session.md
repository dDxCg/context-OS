# 020 — Multi-file, session-scoped rollback

Status: implemented

## Context

[FUTURE.md #1](../agents/FUTURE.md): `ctx rollback <file> -v N` (spec 012) is per-file only.
An agent session realistically touches several context files per run — there is no way to
undo "everything session X did" as one unit, the gap Claude Code's own `/rewind` (atomic
across every file touched since a checkpoint) highlighted by comparison.

Spec 013 already gives every MCP-triggered commit a real, per-session git author:
`agent:{session_id}`. That identity is exactly the "session id" this feature needs — no new
identity concept, just a query and a batch restore built on top of what actor attribution
already writes into every commit.

## Design

**Restore semantics.** For every mirror-repo path `actor_label` ever committed to, across
every watch target, find that actor's **earliest** commit touching the path and restore the
path to the state immediately before it (that commit's parent). This undoes the session's
full net effect on the path, not just its latest edit — matches "undo everything the session
did," not "undo the session's last write." A path the session *created* (earliest commit has
no parent) is deleted, mirror and source both.

**Best-effort, not atomic across repos.** A session's files can span multiple mirror repos
(one per watch target). There is no cross-repo transaction — same limitation
`git_store.py`'s single-writer lock already accepts *per repo*, not across repos. Each
path's restore is atomic on its own (one `write_with_check`/`remove` call); the overall
result reports which paths succeeded and which failed, rather than promising all-or-nothing.

**Reuses the optimistic-concurrency gate spec 012 built** (`write_with_check`): before
restoring a path, read its current `head_rev` and pass it as `expected_rev`. If someone else
committed to that path after the session did but before this rollback ran, the write is
refused (`ConcurrentEditError`) rather than silently discarding that newer edit — the path is
reported failed, everything else still proceeds. This is [FUTURE.md #2](../agents/FUTURE.md)
realized for exactly this one caller, same scoping decision spec 012 made for
`rollback_source`.

**Scope is re-checked per path**, same fail-closed rule live MCP reads/audit reads already
follow (`ARCHITECTURE.md` §6.3) — a path whose scope was revoked since the session ran is
skipped, not restored.

**`"unknown:filesystem"` is refused as a session identifier.** It's the shared fallback
label for *every* untracked edit (spec 007) — not a session, a bucket. Rolling it back would
mean undoing every unattributed edit ever made across the whole system, not "one session."

## Scope

**In**
- `vcs/services/git_store.py`: `commits_by_author(repo_path, author_name)` — every commit in
  a repo whose author name exactly matches `author_name`, oldest first, with the paths each
  commit touched.
- `vcs/services/mirror_path.py`: `repo_path_for(watch_target)` — the mirror repo path for one
  watch target, independent of any specific source path under it (reuses `repo_dir_name`,
  the same computation `resolve_mirror_location` does internally per candidate target).
- `vcs/services/audit.py`: `InvalidSessionActorError`, `rollback_session(actor_label,
  watch_targets=None) -> dict` (`{"rolled_back": [...], "failed": [{"path", "error"}]}`).
- `app/cli/app.py`: `ctx rollback-session <actor_label>`.

**Out**
- True cross-repo atomicity — noted above, structurally not available without a distributed
  transaction this project has no need to build for it.
- Auto-discovering "the current session's actor label" — the caller (a human at the CLI)
  supplies it, read off `ctx history <file>`'s author column. No session registry exists to
  look one up by other means.
- HTTP endpoint for this — matches the existing HTTP API read-only scoping
  (`010-audit-http-api.md`); CLI-only, same as `rollback_source`.

## Acceptance criteria

- AC-1. `commits_by_author(repo_path, author_name)` returns only commits whose author name
  exactly matches `author_name`, oldest first, each with `rev`, `parent` (`None` for a root
  commit), `timestamp`, and `paths` (every path that commit touched).
- AC-2. A commit by a *different* author is excluded from `commits_by_author`'s result.
- AC-3. `rollback_session(actor_label)` restores a path the actor modified (not created) to
  the content immediately before the actor's earliest commit on that path, in both the
  mirror repo (new commit) and the real source file.
- AC-4. `rollback_session(actor_label)` deletes a path the actor *created* (earliest commit
  has no parent) — both the mirror entry and the real source file.
- AC-5. `rollback_session` spans every watch target, not just one — a session that touched
  files under two different watch targets gets both restored.
- AC-6. The result separates `"rolled_back"` (paths successfully restored) from `"failed"`
  (paths that errored), rather than raising on the first failure.

## Error cases

- EC-1. `rollback_session("unknown:filesystem")` raises `InvalidSessionActorError` before
  touching anything.
- EC-2. `rollback_session(actor_label)` for an actor with no commits anywhere returns
  `{"rolled_back": [], "failed": []}` — not an error.
- EC-3. A path whose scope has since been revoked (`is_path_in_scope` now false) is reported
  in `"failed"`, not restored.
- EC-4. A path someone else committed to *after* the session (so its current `head_rev` no
  longer matches what the session last left it at) is reported in `"failed"` via the
  underlying `ConcurrentEditError`'s message — that path's newer content is left untouched,
  and other paths in the same call still proceed.

## Contracts

```python
# vcs/services/git_store.py
def commits_by_author(repo_path: Path, author_name: str) -> list[dict]: ...
# [{"rev": str, "parent": str | None, "timestamp": str, "paths": list[str]}, ...]

# vcs/services/mirror_path.py
def repo_path_for(watch_target: str) -> Path: ...

# vcs/services/audit.py
class InvalidSessionActorError(ValueError): ...
def rollback_session(actor_label: str, watch_targets: list[str] | None = None) -> dict: ...
# {"rolled_back": list[str], "failed": list[{"path": str, "error": str}]}
```

```
ctx rollback-session <actor_label>
```

## Non-goals / open questions

None outstanding.
