# 025 — bare mirror repos, git plumbing instead of porcelain

Status: implemented

## Context

[draft/mirror-storage-optimization-plan.md](../agents/draft/mirror-storage-optimization-plan.md):
`write()`/`remove()`/`move()` currently write through git's porcelain
(`git add`/`git rm`/`git mv`), which requires a working tree. Every
mirrored file's *current* version therefore exists on disk three times: the
real source file, a checked-out copy in the mirror's working tree, and
git's own object-store copy of the same blob. The middle one is pure waste
— existing only because porcelain commands are working-tree commands by
design, not because anything reads it (every read path — `show`,
`log_history`, `diff`, `head_rev`, `commits_by_author` — already goes
through git plumbing/object-store reads, never the working tree).

Fix: initialize new mirror repos bare (`git init --bare`, no working tree
at all) and rewrite `write()`/`remove()`/`move()` to build commits directly
against the object database and index (`hash-object`, `update-index`,
`write-tree`, `commit-tree`, `update-ref`) instead of touching a working
tree file.

## Scope

**In**
- `init_repo()`: `git init --bare` for a genuinely new repo. Existing
  repos (bare or not) are left as-is — no reinitialize-on-every-call, both
  to avoid a redundant subprocess call and because running `git init
  --bare` against a directory that already has a non-bare `.git/`
  subdirectory would create a second, orphaned git-dir alongside it
  (silently hiding the existing history), not convert it.
- `_require_initialized()` (and a new non-raising `_is_initialized()` used
  by `init_repo()`): recognizes either shape — a `.git/` subdirectory
  (old-style, non-bare) or `HEAD`+`objects/` directly under `repo_path`
  (new-style, bare).
- `write()`, `remove()`, `move()`: rewritten on top of plumbing
  (`hash-object -w --stdin`, `update-index --cacheinfo`/`--force-remove`,
  `write-tree`, `commit-tree` with `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env
  vars instead of `--author`, `update-ref HEAD`). No working-tree file is
  read or written by any of them anymore.
- **No migration of existing non-bare mirror repos** — verified
  compatible instead: plumbing writes work correctly against an
  already-existing non-bare repo too (git's index is a plain object-
  database-relative file regardless of bare-ness; a repo whose index
  already reflects HEAD's tree from prior porcelain commits is exactly the
  state plumbing writes build on). Old repos simply stop gaining new
  working-tree updates going forward — their working-tree copy goes stale
  and becomes reclaimable dead weight, not a correctness problem. An actual
  migration/reclaim tool is separate, unbuilt work (the draft's own
  deferred item).

**Out**
- Migrating/converting existing non-bare repos to bare, or reclaiming
  their now-stale working-tree files - separate tool, not this spec.
- Any change to `head_rev`/`show`/`log_history`/`diff`/`commits_by_author`/
  `path_exists_at_rev`/`commit_info`/`write_with_check` - all already
  read/build against the object database, bare-compatible unmodified.
- The alternatives considered in the draft (hardlinks, content-defined
  chunking, git LFS, `ctx gc`) - rejected/deferred there, not revisited.

## Acceptance criteria

- AC-1. `init_repo()` on a path with no repo yet creates a **bare** repo
  (no working-tree files, `HEAD`/`objects` directly under `repo_path`).
- AC-2. `write()` on a fresh bare repo commits content retrievable via
  `show()`, without ever creating `repo_path/relpath` as a real file.
- AC-3. `write()` twice with identical content is a no-op (same rev
  returned, no new commit) - same contract as before, now via tree-SHA
  comparison instead of `git diff --cached --quiet`.
- AC-4. `write()` for two different paths in the same repo results in a
  tree containing **both** - proves the index correctly accumulates
  across calls rather than each write starting from a blank slate.
- AC-5. `remove()` on a tracked path removes it from the tree (verified via
  `path_exists_at_rev` on the new HEAD), and on an already-untracked path
  is a no-op returning `None` - same contract as before.
- AC-6. `remove()` on a directory prefix removes every path under it (the
  `-r`-equivalent of the old `git rm -r`), not just an exact-match entry.
- AC-7. `move()` relocates a tracked path's content to the destination
  (verified via `show()` at the new rev) and removes it from the source
  path, without re-reading the content from disk (uses the existing blob
  SHA from the tree directly).
- AC-8. `move()`/`remove()` on a path not present at HEAD returns `None`
  (no commit), matching the old "nothing to move/remove" contract - now
  checked via tree membership (`path_exists_at_rev`) instead of a raw
  filesystem existence check, since there's no working tree to check.
- AC-9. **Compatibility**: `write()` against a pre-existing *non-bare*
  repo (built the old porcelain way, as every mirror repo created before
  this spec already is) still produces a correct commit, retrievable the
  same way - no forced migration required for old repos to keep working.

## Error cases

- No new error cases - `GitNotAvailableError`/`RepoNotInitializedError`
  behavior is unchanged.

## Contracts

```python
# vcs/services/git_store.py - unchanged public signatures
def init_repo(repo_path: Path) -> None: ...
def write(repo_path: Path, relpath: str, content: bytes, message: str, author: str) -> str: ...
def remove(repo_path: Path, relpath: str, message: str, author: str) -> str | None: ...
def move(repo_path: Path, src_relpath: str, dst_relpath: str, message: str, author: str) -> str | None: ...
```

No caller anywhere (`versioning.py`, `audit.py`, `write_with_check`, the
MCP server, the CLI) changes - this is entirely internal to `git_store.py`.

## Non-goals / open questions

- Existing-repo migration/reclaim tool - separate future spec, per the
  draft.
