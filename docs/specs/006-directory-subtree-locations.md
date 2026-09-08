# 006 — Directory-subtree `locations` rewrite (issues #21/#22)

## Context

[issues.md](../agents/issues.md) #21/#22: `deleted_handle`/`moved_handle`'s
SQL only ever matches a row by exact `location` string. `locations` holds
one row per **file** — a directory has no row of its own — so a directory
delete/move updates zero rows, and every child file underneath keeps stale
`status`/`location` values.

**The git side needs no changes here — it's already correct.** 005 built
`git_store.remove`/`move` with `-r`/`git mv`, which already handle a
directory relpath's whole subtree in one call (verified by
`test_remove_removes_directory_subtree` in 005). `deleted_handle`/
`moved_handle` already pass whatever `relpath` `resolve_mirror_location`
resolves to — a directory relpath included — straight through. This spec is
**SQL-only**: making `locations` catch up to what git already does right.

Design lifted directly from [dir-events-plan.md](../agents/dir-events-plan.md)
§1–2, adapted to the handlers' current (post-005) shape.

**Out of scope, per dir-events-plan.md's own "Known limitations":** deleting
or renaming a *watched source root itself* (not a subdirectory within it) —
that kills the watch entirely, a separate, already-flagged, deliberately
unaddressed problem. This spec's ACs move/delete a subdirectory *within* a
watched source, the realistic case.

## Scope

In scope: subtree-aware `UPDATE` in `deleted_handle` (exact match OR prefix
match, `substr`-based per dir-events-plan.md's false-match reasoning);
subtree prefix-rewrite in `moved_handle`, done *before* the existing
exact-node update, with that exact-node update guarded against a missing
`event.dst` and extended to set `status` from `is_path_in_scope(event.dst)`
— both today's plain-file case (regression) and the new directory case.

Out of scope: git-side changes (none needed); watched-source-root
delete/rename (dir-events-plan.md's flagged limitation).

## Acceptance criteria

AC-1. Given `locations` seeded with `/root`, `/root/a`, `/root/b`, `/other`,
`/rootx` (all `status=1`), when `deleted_handle(db_handler,
DeletedEvent(src="/root"), watch_targets=[...])` is called, then `/root`,
`/root/a`, `/root/b` all go `status=0`, while `/other` and `/rootx` (the
prefix false-match case) stay `status=1`.

AC-2. Given a single tracked **file** (no children), when `deleted_handle`
is called for it, then only that row changes — the subtree clause is a
no-op, matching today's behavior (regression check).

AC-3. Given a directory with children moved to a new path **within
scope**, when `moved_handle(db_handler, MovedEvent(src=old_dir,
dst=new_dir), watch_targets=[...])` is called, then every child row's
`location` is rewritten under the new prefix, `status` stays `1`, and
`st_ino`/`st_dev` are **unchanged** for children (a rename doesn't change
inode; rewriting a directory's inode onto child rows would corrupt
identity).

AC-4. Given the same directory move but the destination is **outside**
every configured source (`is_path_in_scope(dst)` is `False`), when called,
then child rows' `location` is still rewritten, but `status` goes to `0`.

AC-5. Given a plain **file** rename (today's existing case, `dst` exists on
disk), when `moved_handle` is called, then the exact-node update still
rewrites `location`/`st_ino`/`st_dev` by matching inode (regression), *and*
now also sets `status` from `is_path_in_scope(dst)` (the extension
dir-events-plan.md specifies).

## Error cases

EC-1. Given `event.dst` no longer exists on disk by the time `moved_handle`
runs (raced by a second move/delete), when called, then the subtree rewrite
still applies (pure SQL, no filesystem dependency) and the call does not
raise — the exact-node branch degrades to a no-op instead of propagating
`FileNotFoundError`.

## Contracts

```python
# src/vcs/services/versioning.py (behavior change, same signatures as 005)

def deleted_handle(db_handler, event, watch_targets=None): ...
def moved_handle(db_handler, event, watch_targets=None): ...
```

```sql
-- deleted_handle
UPDATE locations SET status = 0
 WHERE location = :path
    OR substr(location, 1, :n) = :prefix
-- prefix = path.rstrip('/') + '/'; n = len(prefix)

-- moved_handle, subtree rewrite (children), run before the exact-node update
UPDATE locations
   SET location = :dst_prefix || substr(location, :cut),
       status   = :active
 WHERE substr(location, 1, :n) = :src_prefix
-- src_prefix/dst_prefix = path.rstrip('/') + '/'; n = len(src_prefix)
-- cut = len(src_prefix) + 1 (SQLite substr is 1-indexed)
-- active = 1 if is_path_in_scope(event.dst) else 0
```

- `is_path_in_scope` imported from `vcs.services.configure` (already
  imported there for `derive_watch_targets`; no new import cycle — confirmed
  in 004, `configure.py` doesn't import `versioning.py`).
- `substr`, not `LIKE` — `LIKE` treats `_` as a single-character wildcard,
  common in directory names (`LIKE '/my_dir/%'` would also match
  `/myXdir/...`).

## Non-goals / open questions

- Watched-source-root delete/rename (kills the watch) — dir-events-plan.md's
  own flagged limitation, unaddressed here too.
- An index on `locations.location` — prefix matching is a full table scan;
  fine at current scale, noted in dir-events-plan.md as a future concern if
  the table grows.
