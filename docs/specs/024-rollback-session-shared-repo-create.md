# 024 — `rollback_session` on a path's first commit in a non-empty shared mirror repo

Status: implemented

## Context

Issue #28 ([issues.md](../agents/issues.md#28-rollback_session-crashes-when-an-actors-earliest-touch-on-a-path-predates-that-paths-own-history-in-a-shared-mirror-repo)):
found live via `scripts/live_integration_test.py`. `rollback_session`
(`audit.py:148`) treats `earliest["parent"] is None` as the only signal that
an actor *created* a path (so it should be deleted rather than reverted to
some prior content). That's correct only when the actor's earliest commit is
the literal first commit in the whole mirror repo's history.

Mirror repos are shared per watch-target *directory*, not per file
(`knowledge.example/docs/` mirrors several baseline files plus whatever an
agent creates later). The first commit that ever touches a *given path* in
such a repo almost always has a non-`None` parent - the repo already has
commits from other files - but that parent commit's tree still doesn't
contain the new path. `git show {parent}:{relpath}` then fails
(`CalledProcessError`, exit 128), crashing `rollback_session` (and the
`ctx rollback-session` CLI command) instead of doing what
`earliest["parent"] is None` already does correctly for the empty-repo case:
delete the path.

The existing regression test (`test_ac4_rollback_session_deletes_path_actor_created`)
only covers a repo where the created file is the *only* file ever written to
it, so `earliest["parent"]` really is `None` there - it could not have caught
this.

## Scope

**In**
- `git_store.py`: `path_exists_at_rev(repo_path, relpath, rev) -> bool` -
  existence check via `git cat-file -e {rev}:{relpath}` (no content read,
  unlike `show()`).
- `audit.rollback_session`: the "did this actor create the path" check
  becomes `earliest["parent"] is None or not path_exists_at_rev(repo_path,
  relpath, earliest["parent"])` - covers both the empty-repo case (unchanged
  behavior) and the shared-repo case (the fix).
- Regression test: an unrelated file committed first (different author),
  then the target actor creates a brand-new path in the *same* repo;
  `rollback_session` for that actor must delete the path, not crash.

**Out**
- Any change to the "actor modified an existing path" branch (`else:`) -
  unaffected, already correct.
- `rollback_source` (spec 012) - single-path, does not do this
  create-vs-modify branching at all.

## Acceptance criteria

- AC-1. `rollback_session` for an actor whose earliest (and only) commit on
  a path is that path's first-ever commit in a mirror repo that already has
  unrelated commit history deletes the path (mirror + real source file),
  the same outcome as the already-correct empty-repo case, instead of
  raising `CalledProcessError`.
- AC-2. `path_exists_at_rev(repo_path, relpath, rev)` returns `False` for a
  `relpath` not present in `rev`'s tree, `True` when it is present.

## Error cases

- None new - `rollback_session`'s existing `OutOfScopeError`/
  `ConcurrentEditError` handling is unchanged; this only fixes what happens
  after those checks pass, in the create-vs-modify branch.

## Contracts

```python
# vcs/services/git_store.py
def path_exists_at_rev(repo_path: Path, relpath: str, rev: str) -> bool:
    """Whether relpath existed in rev's tree - `git cat-file -e {rev}:{relpath}`,
    exit 0 means yes, non-zero means no (not raised - a normal, expected
    outcome for the actor-created-this-path case, not an error)."""
```

## Non-goals / open questions

- None outstanding.
