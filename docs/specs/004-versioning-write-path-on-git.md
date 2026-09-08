# 004 — `created_handle`/`modified_handle` write through git, not SQL versions/blobs

## Context

001–003 built `git_store` and `mirror_path` but nothing in the running
system calls them yet — `versioning.py`'s handlers still write to the
`versions` SQL table and `BLOB_DIR`. Per the user's explicit direction
(full replace, not dual-write), this spec cuts `created_handle` and
`modified_handle` over to git as the version store, per
[git-backend-plan.md](../agents/git-backend-plan.md)'s original design.

**Scope note:** `deleted_handle`/`moved_handle` are deliberately **not**
included here — they need new `git_store` primitives (`remove`, `move`)
this spec doesn't build, plus the directory-subtree behavior from
[issues.md](../agents/issues.md) #21/#22 that's explicitly out of scope
until its own spec. Bundling all four handlers into one cycle would violate
AGENTS.md §1.5 ("one behavior per cycle") — the write path
(`created`/`modified`) is one cohesive behavior change; delete/move is a
materially different one needing new mechanism.

**This changes existing tests.** `test_versioning.py` currently asserts
`versions`-table rows and `BLOB_DIR` files for `created_handle`/
`modified_handle` — five tests need rewriting to assert git state instead.
Per AGENTS.md §1.3, this is legitimate because the spec changed first (this
document, approved) — the tests follow the spec, not the other way around.
`deleted_handle`/`moved_handle`'s two tests are untouched (out of scope).

## Scope

In scope: `created_handle` and `modified_handle` write file content via
`git_store`/`mirror_path` instead of `versions`/`BLOB_DIR`; `contexts`/
`locations` SQL bookkeeping (identity, status) is unchanged — git only
replaces content history, not identity tracking. A `watch_targets`
parameter (optional, defaults to a live `derive_watch_targets()` call) is
added to both handlers so tests can inject an isolated watch-target list
without needing a real `config.yaml`.

Out of scope: `deleted_handle`, `moved_handle`, actor-identity resolution
(hardcoded `"unknown:filesystem"` for now, per
[actor-attribution.md](actor-attribution.md)'s documented default — that
spec isn't built, this just uses its already-decided fallback string),
dropping the `versions` table from `data/schema.sql` (left in place, simply
unwritten-to going forward — a separate cleanup, not required for this
behavior change).

## Acceptance criteria

AC-1. Given a new file with no existing context, when
`created_handle(db_handler, CreatedEvent(src=path), watch_targets=[dir])` is
called, then: a `contexts` row and a `locations` row (`status=1`) exist for
it, the mirror repo for `dir` has exactly one commit whose content matches
the file, and **no row exists in `versions`**.

AC-2. Given `created_handle` is called twice for the same file with
unchanged content (e.g., a delete-then-recreate cycle), when called, then
the mirror repo gains no duplicate commit — git's own no-op detection
(001 AC-3) — while the `locations` row is still correctly re-activated.

AC-3. Given a tracked file whose new content's similarity to what's
currently committed falls **below** `NEW_VERSION_THRESHOLD`, when
`modified_handle(db_handler, ModifiedEvent(src=path), tmp_file,
watch_targets=[dir])` is called, then the mirror repo gains exactly one new
commit and **no row is written to `versions`**.

AC-4. Given a tracked file whose new content's similarity is **above**
`NEW_VERSION_THRESHOLD` (a near-identical edit), when `modified_handle` is
called, then the mirror repo's commit count is unchanged — same
similarity-gate policy as today, now checked against the mirror's working
tree content directly (`repo_path/relpath`) instead of a `BLOB_DIR` file.

AC-5. Given `modified_handle` is called for a path with no tracked context
(`context_id is None`), when called, then it delegates to `created_handle`
exactly as today, and the file ends up committed via that path.

## Error cases

EC-1. Given `created_handle`/`modified_handle` is called with a
`source_path` not under any entry in `watch_targets`, the call propagates
`mirror_path.PathNotWatchedError` rather than silently no-oping or writing
to a wrong location — this should be unreachable via the real watcher
(which only ever fires for paths already in scope), so a loud failure here
is correct, not something to swallow.

## Contracts

```python
# src/vcs/services/versioning.py (changed signatures)

def created_handle(db_handler: DBHandler, event: CreatedEvent, watch_targets: list[str] | None = None) -> None: ...
def modified_handle(db_handler: DBHandler, event: ModifiedEvent, tmp_file: TempFile, watch_targets: list[str] | None = None) -> None: ...
```

- `watch_targets=None` (the default every real caller uses) triggers a live
  `configure.derive_watch_targets()` call, matching how `is_path_in_scope`
  already re-parses config per call elsewhere in this codebase — no new
  caching behavior introduced.
- Commit author: hardcoded `"unknown:filesystem"` literal for both handlers
  in this spec — not a dependency on the unbuilt actor-attribution spec,
  just reusing its already-decided default string.
- Commit message: `f"created {relpath}"` / `f"modified {relpath}"`.
- Similarity check in `modified_handle` reads current content from
  `repo_path / relpath` (the mirror's working tree already has it checked
  out) rather than `BLOB_DIR / f"{hash}.blob"` — no separate blob-read
  helper needed.
- `_append_context`'s existing identity bookkeeping (location sync,
  reactivation, contexts/locations insert) is preserved; only the
  `versions`-row-insert and hash-based dedup (`_check_existed_version`,
  redundant once git's own no-op-commit detection covers it) are removed
  from the write path.

## Files

| File | Change |
|---|---|
| `vcs/services/versioning.py` | `created_handle`/`modified_handle` call `git_store`/`mirror_path`; `_check_existed_version`, `_get_version_hash`, `_check_current_version`, `_append_version` become dead once their only callers are gone — removed as a post-green refactor step, not left as unreachable code |
| `tests/unit/vcs/services/test_versioning.py` | 5 existing tests rewritten to assert mirror-repo/git state instead of `versions`/`BLOB_DIR`; `deleted_handle`/`moved_handle` tests untouched |
| `tests/fixtures/*` | new `isolate_git_repo_dir(tmp_path, monkeypatch)` fixture, patching `mirror_path.GIT_REPO_DIR` — same pattern as the existing `isolate_blob_dir` patching `versioning.BLOB_DIR` |

## Non-goals / open questions

- `deleted_handle`/`moved_handle` git wiring — next spec, needs `git_store`
  to grow `remove`/`move` primitives first.
- Dropping `versions`/`BLOB_DIR` from `data/schema.sql`/disk entirely —
  left as dead-but-present; a cleanup spec once nothing reads them (`audit.py`
  doesn't yet, since it's still a stub).
- Threading real `watch_targets` from `LocalRuntime` down through
  `ConsumerWorker`/`LocalConsumer` in production (today's callers keep using
  the `None` default, i.e. a live re-parse per event) — no caching added
  here, matching existing per-call-reparse precedent; revisit only if this
  turns out to be a real hot path, not speculatively.
