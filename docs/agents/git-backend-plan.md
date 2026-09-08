# Storage backend: swap the SQLite blob store for git

Status: **proposed, not started.** Follow-up to
[competitive-landscape.md](competitive-landscape.md), which found no comparable
project reimplements version storage from scratch the way this repo currently
does — the two that version at all ([palinode](https://github.com/Paul-Kyle/palinode),
[GitAgent](https://github.com/open-gitagent/gitagent)) both delegate to git.

## Context — what's being reimplemented today

`content_hash` + `NEW_VERSION_THRESHOLD` similarity gate + a flat `BLOB_DIR` of
`{hash}.blob` files + a `versions` table keyed by `(context_id, version_number)`
([vcs/shared/config.py](../../src/vcs/shared/config.py),
[vcs/services/versioning.py](../../src/vcs/services/versioning.py),
[data/schema.sql](../../data/schema.sql)) is a hand-rolled, weaker version of
what a git object store already does: content-addressed storage, dedup,
diffing, and a commit graph as history. `vcs/services/audit.py` — the
`history`/`rollback`/`diff` surface that's the actual product differentiator
(per [competitive-landscape.md](competitive-landscape.md)) — is a stub with
nothing to build *because* that layer doesn't exist yet on top of the SQL
schema. Building it on git instead of finishing it on SQL avoids writing a
second, worse version of `git log`/`git show`/`git diff`.

Concretely, git eliminates or shrinks four open items:

| Issue | Today | Under git |
|---|---|---|
| [#17](issues.md) blob never written on the watcher path | `modified_handle` writes a blob; `created_handle`→`_append_context` never does | Every path in the mirror tree, source or watcher, goes through one `git add`+commit call — no second code path to forget |
| [#18](issues.md) no same-hash guard, versions duplicate | Missing check in `modified_handle` | `git commit` on an unchanged path is a no-op by construction (empty diff → nothing to commit) |
| [#21](issues.md) dir delete leaves child rows active | Needs bespoke `substr` prefix-match SQL, platform-dependent (`is_dir` unreliable on Windows deletes) | Check whether the *mirror* path is still a directory (it is — only the source side is gone) and `git rm -r` it; no dependency on the event's `is_dir` at all |
| [#22](issues.md) dir move is a silent no-op | Needs a prefix-rewrite `UPDATE` over `locations` | `git mv <mirror_src> <mirror_dst>` moves the whole subtree and preserves history through the rename, same one-liner for a file or a directory |

The multi-file atomic rollback gap flagged in
[competitive-landscape.md](competitive-landscape.md) also falls out for free:
a commit that batches everything one debounce window (or one MCP-approval
burst) produced *is* the atomic rollback unit — `git checkout <rev> -- .`
(scoped to the paths that commit touched) restores all of them together.

## Design

### One mirror working tree, not N source directories

Sources are scattered, file-granular, and can be on different drives
(`derive_watch_targets` in [configure.py](../../src/vcs/services/configure.py)
already has to handle this). Git needs one working tree, so watched content is
mirrored into `GIT_REPO_DIR`, anchored the same way as `BLOB_DIR` today
([vcs/shared/config.py](../../src/vcs/shared/config.py)):

```python
GIT_REPO_DIR = Path(anchored(os.getenv("GIT_REPO_DIR", "data/repo")))
```

Path mapping is deterministic and reversible, reusing `path_normalize`'s
existing posix-style output (`C:/Users/...`, per
[issues.md](issues.md) #11):

```
to_mirror_path("C:/Users/admin/docs/x.txt") -> GIT_REPO_DIR / "C/Users/admin/docs/x.txt"
to_source_path(mirror_relpath)              -> the inverse
```

The existing `locations` table identity layer (`st_ino`/`st_dev` →
`context_id`) is **kept, not dropped** — git tracks by path, not inode, so
rename detection at the git-object level is still just a heuristic at diff
time. Keeping the inode-keyed lookup is what lets `moved_handle` find *which*
mirror path to `git mv`, exactly as it finds which `locations` row to update
today.

### What each handler does

| Handler | Today | Under git |
|---|---|---|
| `created_handle` | insert `contexts`/`locations`/`versions` rows | copy content to `to_mirror_path(src)`, `git add`, commit |
| `modified_handle` | similarity-gated blob write + `versions` row | copy new content over the mirror path; commit only if `git diff --quiet` reports a change (replaces the `_decide_to_append_version` blob-existence + similarity check with git's own diff) |
| `deleted_handle` | `UPDATE locations SET status = 0` (exact match only — #21) | if `to_mirror_path(src)` is a directory in the mirror, `git rm -r`; otherwise `git rm` the file. No branch on `event.is_dir` |
| `moved_handle` | `UPDATE ... WHERE context_id = ?` (fails for directories — #22) | `git mv <mirror_src> <mirror_dst>`, one call whether the moved path is a file or a directory |

`locations.status` keeps its current meaning (in scope / not) — that's
config-scope bookkeeping, orthogonal to git and unaffected by this change.

### Commit granularity

Reuse the watcher's existing 0.5s debounce window
([local_watcher.py](../../src/vcs/workers/local/local_watcher.py)) as the
commit-batching window: every event the consumer thread drains in one pass
becomes one commit. This is deliberately a scope-contained choice — it
preserves today's behavior (same debounce constant, same
`NEW_VERSION_THRESHOLD` gate stays as-is for `modified_handle` if kept, see
"Open question" below) rather than bundling a versioning-*policy* change into
a storage-*backend* swap.

### Concurrency — single writer is mandatory

`.git/index.lock` does not arbitrate between concurrent writers the way
SQLite's WAL mode does — a second process attempting to commit while one is
in progress fails outright rather than queuing. Today's `LocalRuntime` already
runs source events and config events on two separate threads
([local_runtime.py](../../src/vcs/workers/local/local_runtime.py), Option A
from [issues.md](issues.md) #1), both of which call into
`vcs/services/versioning.py`. Under git, **both must funnel through one lock**
around every write operation (`git add`/`git rm`/`git mv`/`git commit`) —
add a single `threading.Lock` (or a dedicated writer thread the other two hand
work to) around the new storage layer. This is a new constraint that doesn't
exist today, since SQLite's own locking already covered this case.

The CLI (`ctx rollback`/`history`/`diff`, per
[cli-plan.md](cli-plan.md)) reading `git log`/`git show` concurrently with the
daemon writing is safe — git reads don't need the lock. A CLI-initiated
**rollback**, though, is a write, and must not run against the same working
tree the daemon might be mid-commit on. Until the CLI has a control channel to
the daemon (open item in [cli-plan.md](cli-plan.md)), route rollback through
the daemon rather than having the CLI touch the mirror repo directly.

### Rollback / diff / history (`audit.py`)

```python
def get_version_list(path):        # git log --follow -- <mirror_path>
def check_diff(path, v1, v2):      # git diff <v1> <v2> -- <mirror_path>
def rollback_source(path, version):
    # git checkout <version> -- <mirror_path>, then copy the restored content
    # back to the real source path (locations.location), under the writer lock
```

Multi-file/session rollback (the gap identified in
[competitive-landscape.md](competitive-landscape.md)) becomes:
`rollback_batch(commit_sha)` → `git checkout <commit_sha> -- .` restricted to
the paths that commit's diff touched, then copy each restored file back to its
mapped source location.

**Scope re-check still applies.** Per the same doc's gap #6: `rollback_source`
and `get_version_list` must call `is_path_in_scope` before touching history,
same fail-closed rule as live MCP reads — git doesn't grant or remove that
boundary on its own.

### Migration of existing data

One-time script: for each `context_id`, replay its `versions` rows in
`version_number` order as commits into the mirror repo, pulling content from
`BLOB_DIR / f"{content_hash}.blob"`. The 4 blob-less versions already known
from [issues.md](issues.md) #17 (verified against `data/db-dev.sqlite`) can't
be migrated — they become gaps in git history, same unrecoverable status they
have today. [issues.md](issues.md) #18's duplicate version pairs collapse
naturally: committing identical content twice is a no-op, so replaying them
produces one commit, not two — free cleanup as a side effect of migrating.

### Library choice

Recommend shelling out to the system `git` binary via `subprocess`, not
`dulwich` (pure-Python git). Reasoning: `git mv`/`git rm -r`/porcelain diff
output are exactly the primitives this needs, well-tested, and avoid
reimplementing rename/tree semantics against a library API. Cost: adds a `git`
binary as a runtime prerequisite (document next to the Python version
requirement in [README.md](../../README.md)) — verify it's available in CI
(GitHub Actions Windows/Linux runners ship it by default, but confirm rather
than assume). Revisit `dulwich` only if the project ever needs to ship without
assuming an external binary (e.g., a single packaged executable).

## Open question — keep the similarity gate, or trust git's diff?

`NEW_VERSION_THRESHOLD` (0.9) exists today to avoid recording a new version
for near-identical edits. Git's own `diff --quiet` is a stricter, purely
byte-level check — any change commits. Two options, not resolved here:

1. **Keep the similarity gate** in `modified_handle` exactly as today, and let
   it decide *whether* to write+commit at all — git only replaces the storage
   mechanism once that decision is made. Lowest risk, preserves current
   test expectations and behavior.
2. **Drop it**, let git's own diff be the sole gate. Simpler code, but changes
   observable behavior (near-duplicate edits now do get a version), and is a
   policy change bundled with a backend swap — the thing this plan explicitly
   tries to avoid doing in the same step.

Recommend (1) for the initial swap; revisit (2) separately if it turns out to
matter in practice.

## Files

| File | Change |
|---|---|
| `src/vcs/services/git_store.py` | **new** — repo init, `to_mirror_path`/`to_source_path`, `write`/`remove`/`move`/`commit`, `log`/`show`/`diff`, the single writer lock |
| [vcs/services/versioning.py](../../src/vcs/services/versioning.py) | handlers call `git_store` instead of direct blob I/O; identity lookups (`_get_context_id_by_location` etc.) unchanged |
| [vcs/shared/config.py](../../src/vcs/shared/config.py) | add `GIT_REPO_DIR`; `BLOB_DIR` retired once migration completes |
| [data/schema.sql](../../data/schema.sql) | drop `versions` table (git log is the version table); keep `contexts`/`locations` for identity + scope status + mirror-path mapping |
| [vcs/services/audit.py](../../src/vcs/services/audit.py) | implement `get_version_list`/`check_diff`/`rollback_source` against `git_store`, plus new `rollback_batch` |
| [README.md](../../README.md) | add `git` binary as a prerequisite alongside the Python version requirement |

## Tests

- `tests/unit/vcs/services/test_git_store.py` — new, using a temp-dir repo
  fixture (`git init` per test, matching the pattern `db_handler`/`seeder`
  fixtures already use for SQLite).
- `tests/unit/vcs/services/test_versioning.py` — update to assert against
  `git_store` calls instead of blob-file existence.
- Directory delete/move cases from
  [dir-events-plan.md](dir-events-plan.md) get **simpler**, not harder: no
  platform-conditional fixtures needed, since the mirror-side directory check
  replaces the `is_dir` dependency entirely. Re-verify the same scenarios
  (delete a directory, move a directory in/out of scope) against the git
  backend rather than dropping them.
- Concurrency: a stress test publishing rapid source + config events from two
  threads simultaneously, asserting no `index.lock` failure — this is the one
  genuinely new failure mode git introduces that SQLite's WAL didn't have.

## Verification

```powershell
uv run pytest -q
uv run ruff check .
```

Live, extending the existing scratchpad pattern (`live_test.py`, per
[pubsub-plan.md](pubsub-plan.md)):

1. Create, modify, delete, move a file — confirm one commit per debounced
   batch, `git log` matches the event sequence.
2. Rename a directory — confirm `git mv` on the mirror preserves history
   (`git log --follow` on a moved child file still shows pre-move commits).
3. Delete a directory on Windows (the platform [issues.md](issues.md) #21
   flagged as silently broken today) — confirm the mirror-side directory
   check removes the whole subtree despite the single, childless
   `FileDeletedEvent` Windows emits.
4. Two rapid MCP-guardrail approvals in different directories — confirm no
   `index.lock` contention between the source and config consumer threads.
5. `ctx rollback`/`history`/`diff` (once implemented) against a file whose
   scope was since revoked — confirm fail-closed, matching live MCP
   enforcement.

## Not done here

- **git gc / repack scheduling.** Standard git housekeeping applies; not a new
  design problem, just an operational one to schedule later.
- **Encrypting the mirror repo.** Same exposure as today's plaintext
  `BLOB_DIR` — not a regression, not addressed here either.
- **Cross-file content dedup.** Preserved, not lost: git's object store hashes
  blob *content*, not path, so two different files with identical bytes still
  share one object regardless of where they live in the mirror tree — the
  same property `BLOB_DIR`'s `{hash}.blob` naming gives today.
