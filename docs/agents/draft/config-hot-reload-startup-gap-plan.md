# Draft — config hot-reload: close the startup gap

Status: draft, not started. README/README.vi mark "Config hot-reload" as
**In progress**, not Done — this is the concrete reason why, found by reading
`vcs/initialize.py` against what the *running-daemon* path
(`ConfigConsumer`, see [ARCHITECTURE.md](../../ARCHITECTURE.md) §4.3)
actually does.

## The gap, confirmed by reading the code

Two separate code paths apply `config.yaml` to the mirror, and they don't do
the same thing on removal.

**While the daemon is running** (`config_consumer.py:47-77`): a
`config.yaml` edit is diffed against the last snapshot
(`get_config_diff()`), and *both* directions are applied symmetrically —
`created_handle()` per added file, **`deleted_handle()` per removed file**
(which calls `git_store.remove()` — an actual commit recording the
removal). Snapshot is stored last, after the diff was applied.

**At daemon startup** (`vcs/initialize.py:24-36`, `Initializer.init()`):

```python
store_config_snapshot()                              # baseline reset FIRST
sync_source_status(self.db_handler, sources=self.sources)
for source in self.sources:
    if source["type"] == "local":
        adapter = LocalAdapter(self.db_handler)
        adapter.local_processing(source["path"])      # re-scan, add only
```

- `store_config_snapshot()` runs **unconditionally, before any diff** — so
  by the time anything could compare "old vs new," the baseline has already
  been overwritten to match the current file. Any diff computed after this
  point is always empty.
- `local_processing()` (`local_adapter.py:53-61`) walks every *current*
  source and calls `_append_context()` per file — this is the add side, and
  it's fine: idempotent, `git_store.write()` no-ops on identical content.
- `sync_source_status()` (`versioning.py:211-235`) deactivates every
  location (`status = 0`), then reactivates only the ones under a *current*
  source. This correctly flips DB status for a source removed while the
  daemon was offline — **but it is pure SQL, no `git_store` call at all.**

Net effect: remove a source from `config.yaml` while the daemon is offline,
restart it — `locations.status` correctly goes to `0`, but the file's mirror
repo is never `git rm`'d. The mirror's `HEAD` keeps the stale content
forever, with no commit ever recording that it left scope. Same edit made
while the daemon is *running* produces an honest "moved out of scope" /
"deleted" commit (`versioning.py`'s `deleted_handle`/`moved_handle` call
sites in `config_consumer.py:64-66`). Two different outcomes for the same
config edit, depending purely on whether the daemon happened to be up when
it was made.

This isn't cosmetic — it's the same "git history is the audit trail" premise
[item 8's draft](audit-tracing-log-plan.md) leans on. A reviewer running
`ctx history`/`ctx diff` on a since-removed source's mirror sees content
that looks live (no removal commit, no marker) when it's actually orphaned.

## Why disk-walking the removed paths (copying ConfigConsumer's pattern) isn't enough

The obvious fix is "call `get_config_diff()` before `store_config_snapshot()`
at startup too, then loop `diff["deleted"]` through `collect_files()` +
`deleted_handle()`, exactly like `config_consumer.py` already does." That's
half-right — same shape as the working live path — but `collect_files()`
(`helper.py:99-112`) returns `[]` for a path that no longer exists
(`if not p.exists(): return []`, line 103). If the *entire removed source
directory* is also gone from disk (not just dropped from config.yaml — e.g.
someone deleted the folder while the daemon was down too), there's nothing
left to walk, so no `deleted_handle()` calls happen for any file under it,
same silent gap as today. `config.yaml`-only removal (directory still
present, just unlisted) would be fixed; directory-also-gone would not.

## Recommended fix: reconcile from the DB, not the disk

`sync_source_status()` already computes exactly the right set as a
byproduct — every location that *was* `status = 1` before this boot and is
*not* among the paths just reactivated for a current source is precisely
"left scope while we were down." That set exists in SQLite regardless of
whether the underlying directory still exists on disk, which is what makes
it strictly more robust than re-deriving it from `get_config_diff()` +
`collect_files()`.

Sketch (exact function boundaries are a spec-time call, not decided here):

1. Before `sync_source_status()` flips anything, snapshot the currently-`1`
   locations (`SELECT location FROM locations WHERE status = 1`).
2. Run `sync_source_status()` as today (status flips happen the same way).
3. Diff: `about_to_deactivate = old_active - still_active_after`.
4. For each path in `about_to_deactivate`, resolve its mirror
   (`resolve_mirror_location`) and call `git_store.remove()` with an actor
   label distinct from both `unknown:filesystem` and `startup:scan` —
   something like `startup:reconcile` — so the commit honestly says *why*
   the removal happened (source dropped while offline), not "someone edited
   a file." `local_adapter.py`'s `STARTUP_ACTOR_LABEL` pattern
   (`local_adapter.py:15-16`) is the template to follow.
5. `store_config_snapshot()` still needs to run at some point to keep the
   diff baseline current for the *next* boot and for the live
   `ConfigConsumer` path — ordering relative to step 4 needs to land wherever
   doesn't reopen the same "baseline overwritten before it's used" bug this
   plan started from.

`PathNotWatchedError` from `resolve_mirror_location` is expected for some of
these (a location whose *entire* watch target directory also vanished has no
mirror to resolve against anymore, or already had one under a now-unrelated
target) — needs a graceful skip, not a crash, at spec time.

## Open questions (spec-time, not decided here)

- Does step 4's `git_store.remove()` run inside the same transaction/order
  as `sync_source_status()`'s DB write, or after it commits? A crash
  between the two would leave DB and mirror disagreeing again — the exact
  failure mode this plan exists to close. Needs a decision, not an
  assumption.
- Actor label naming (`startup:reconcile` vs reusing `startup:scan` vs
  something else) — cosmetic but shows up in every `ctx history` line
  forever once chosen.
- Whether this should be one spec or folds into whatever eventually
  addresses [FUTURE.md item 5](../FUTURE.md) (self-heal for a dropped
  watcher event) — related (both are "daemon was down, reality drifted"),
  but item 5 is about *content* drift (a missed watcher event on a live,
  still-configured source) while this is about *scope* drift (a source
  that left `config.yaml` entirely while offline). Different trigger,
  arguably the same "startup reconcile" home — decide at spec time whether
  splitting them is worth it under `AGENTS.md`'s one-behavior-per-cycle
  rule, or whether they're small enough to share a spec.

## Not done here

- No code changed. No spec written. This is scoping only, per the
  "spec gets written only once someone is about to implement it" rule.
- Whether `local_processing()`'s add-side (already broad, rescans
  everything unconditionally) needs any change — reading the code, it
  doesn't: it's already idempotent and already a superset of the diff's
  `added` set, so it's arguably *more* correct than the live path, not
  less. Only the removal side has a real gap.
