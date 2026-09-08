# 005 — `deleted_handle`/`moved_handle` on git (file case)

## Context

004 wired the write path (`created_handle`/`modified_handle`) to git. This
spec does the same for the other two: `deleted_handle`/`moved_handle`, plus
the two new `git_store` primitives (`remove`, `move`) they need — same
combined shape as 004's write-path spec, since delete+move is one cohesive
behavior change (git-side removal/rename) the way create+modify was one
cohesive write-path change.

**Scope note: file case only, no directory subtree.** [issues.md](../agents/issues.md)
#21/#22 (directory delete/move) need `locations` SQL updated across a whole
subtree (a prefix-rewrite, per [dir-events-plan.md](../agents/dir-events-plan.md))
in addition to whatever the mirror side does — that's a materially different
behavior (multi-row SQL change vs. single-row) and its own spec once this one
lands. This spec keeps today's exact-match-only SQL behavior for both
handlers unchanged; it only swaps the git-side content-history mechanism.

## Scope

In scope: `git_store.remove`, `git_store.move` (new primitives, same shape
as `write`/`head_rev`); `deleted_handle`/`moved_handle` call them; a
`watch_targets` parameter added to both (matching 004's pattern) so tests
can inject an isolated list.

Out of scope: directory-subtree SQL rewrite (#21/#22 — next spec), any
change to `locations`' exact-match `UPDATE`/inode-lookup logic.

## Acceptance criteria

AC-1. Given a tracked file moved within one watch-target boundary (same
mirror repo for both `src` and `dst`), when `moved_handle(db_handler,
MovedEvent(src=..., dst=...), watch_targets=[dir])` is called, then the
mirror repo has the file committed under the new relpath (`git mv`,
preserving history — `git log --follow` on the new path shows the
pre-move commit), and the old relpath is no longer present in the working
tree.

AC-2. Given a tracked file deleted, when `deleted_handle(db_handler,
DeletedEvent(src=path), watch_targets=[dir])` is called, then the mirror
repo no longer has the file in its working tree, but the removal itself is
a commit (`head_rev` for that path still resolves to the removal commit,
not `None` — deletion is a recorded event, not erasure of history).

AC-3. Given a file moved **between two different** watch-target boundaries
(two different mirror repos), when `moved_handle(...)` is called, then the
destination repo gains a commit with the file's content and the source
repo gains a removal commit — `git mv` can't span repos, so this is a
write-then-remove fallback, verified as two separate repos both correctly
updated.

AC-4. Given `deleted_handle` is called for a path with nothing tracked in
the mirror (already removed, or never versioned), when called, then it does
not raise and creates no empty commit.

## Error cases

EC-1. Given `deleted_handle`/`moved_handle` is called with `event.src` not
under any entry in `watch_targets`, the call propagates
`mirror_path.PathNotWatchedError` — same defensive-only expectation as
004's EC-1.

## Contracts

```python
# src/vcs/services/git_store.py (additions)

def remove(repo_path: Path, relpath: str, message: str, author: str) -> str | None:
    """git rm -r --ignore-unmatch (works uniformly for a file or a directory
    subtree, and no-ops cleanly if nothing was tracked there) + commit.
    Returns the new rev, or None if nothing was actually removed."""

def move(repo_path: Path, src_relpath: str, dst_relpath: str, message: str, author: str) -> str | None:
    """git mv src_relpath dst_relpath + commit. Returns the new rev, or None
    if src_relpath doesn't exist on disk (nothing to move)."""
```

```python
# src/vcs/services/versioning.py (changed signatures)

def deleted_handle(db_handler: DBHandler, event: DeletedEvent, watch_targets: list[str] | None = None) -> None: ...
def moved_handle(db_handler: DBHandler, event: MovedEvent, watch_targets: list[str] | None = None) -> None: ...
```

- `remove`'s `-r --ignore-unmatch` is deliberately the same "no branching on
  whether the target is a file or a directory" mechanism
  [git-backend-plan.md](../agents/git-backend-plan.md) specified — `-r` is
  harmless on a single file, `--ignore-unmatch` makes an untracked/already-gone
  path a clean no-op instead of a `CalledProcessError`. No-op detection
  reuses the same `git diff --cached --quiet` check `write()` already uses.
- `move`'s cross-repo fallback in `moved_handle`: read `event.dst`'s content
  from disk (it already exists there — the OS move already happened by the
  time the event fires), `git_store.write` it into the destination repo,
  then `git_store.remove` the source relpath from the source repo.
- Commit messages: `f"deleted {relpath}"`, `f"moved {src_relpath} to
  {dst_relpath}"` (same-repo) or the write/remove pair's own messages
  (cross-repo). Author: `DEFAULT_AUTHOR` (same literal 004 introduced).

## Non-goals / open questions

- Directory-subtree SQL rewrite (#21/#22) — next spec, needs both this
  spec's `remove`/`move` primitives (which already handle a directory
  subtree correctly on the *git* side via `-r`) and a `locations`
  prefix-rewrite on the *SQL* side that this spec deliberately doesn't
  touch.
- Whether `remove`'s commit should also fire when the file existed but was
  already uncommitted (a write that never got committed for some reason) —
  not reachable given `write()`'s own guarantees, not tested here.
