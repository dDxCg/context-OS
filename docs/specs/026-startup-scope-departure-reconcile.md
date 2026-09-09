# 026 — reconcile mirror on a source dropped while the daemon was offline

Status: implemented

## Context

[draft/config-hot-reload-startup-gap-plan.md](../agents/draft/config-hot-reload-startup-gap-plan.md):
while the daemon is running, removing a source from `config.yaml` is diffed
by `ConfigConsumer` and applied symmetrically — `deleted_handle()` per
dropped file, an honest git commit recording the removal
(`config_consumer.py:64-66`). At startup, `Initializer.init()` calls
`store_config_snapshot()` **before** any diff is possible, then
`sync_source_status()` only flips `locations.status` to `0` in SQLite — no
`git_store` call at all. A source removed from `config.yaml` while the
daemon was offline leaves its mirror repo's `HEAD` holding stale content
forever, with no commit ever recording the departure — breaking the "git
history is the audit trail" premise every other read path in this project
relies on.

## Scope

**In**
- `Initializer.init()`: read the *previous* config snapshot (before it gets
  overwritten) to derive the watch targets that were in effect before this
  boot, and to capture which locations were `status = 1` before
  `sync_source_status()` runs.
- A new `versioning.py` function that, given the previously-active location
  set, the still-active set after `sync_source_status()`, and the old watch
  targets, issues one `git_store.remove()` per location that left scope —
  same shape as `deleted_handle()`'s git call, distinct actor label
  (`startup:reconcile`, not `startup:scan` or `unknown:filesystem`) so
  `ctx history` honestly shows *why* the removal happened.
- Reordering inside `Initializer.init()` so `store_config_snapshot()` runs
  **after** the old snapshot has been read and the reconcile has resolved
  mirror paths against it — not before, which is the root cause of today's
  gap.

**Out**
- The directory-also-deleted-from-disk case (source removed from
  `config.yaml` *and* its whole directory gone from disk while offline):
  `derive_watch_targets()` re-walks disk (`_nearest_existing_dir`) and can
  resolve to a different (broader, wrong) ancestor than the one actually
  used historically, or `resolve_mirror_location` can raise
  `PathNotWatchedError`. Both degrade to a skipped reconcile for that
  location (logged, not raised) rather than a wrong commit landing in an
  unrelated mirror repo. Overlaps [FUTURE.md item
  5](../agents/FUTURE.md) (self-heal for a dropped watcher event) — not
  solved here.
- Any change to the *live*, daemon-running removal path
  (`config_consumer.py`) — already correct, untouched.
- Any change to the add/backfill side of startup (`local_processing()`) —
  already idempotent and already a superset of the diff's `added` set, per
  the draft's own read of the code.
- Concurrent `config.yaml` writes from CLI/MCP/daemon racing each other —
  accepted hazard, documented elsewhere (`FUTURE.md` item 9), not this
  spec's concern.

## Acceptance criteria

- AC-1. Given a source present in the last stored config snapshot but
  absent from the current `config.yaml` (removed while the daemon was
  down), when `Initializer().init()` runs, then every location that was
  `status = 1` under that source and is not covered by any current source
  has its mirror content removed at the new `HEAD`
  (`git_store.path_exists_at_rev` returns `False`), same outcome as the
  live `ConfigConsumer` removal path.
- AC-2. The commit created by AC-1's removal has an author distinct from
  `unknown:filesystem` and from `startup:scan` — identifiable as a
  startup-driven scope departure, not a normal edit or a fresh backfill.
- AC-3. Given no source was removed since the last snapshot (config
  unchanged, or only sources added), `init()` issues zero `git_store.remove()`
  calls — no spurious commits on an ordinary/first-run boot.
- AC-4. Given a location that left scope but whose mirror repo has no
  commit for it at all yet (e.g. it was only ever backfilled, never
  actually committed for some other reason), the reconcile step is a no-op
  for that location — same "nothing to remove" contract `deleted_handle`
  already has via `git_store.remove()` returning `None`.
- AC-5. `store_config_snapshot()` still runs once per `init()` call and its
  result still reflects the *current* `config.yaml` (unchanged observable
  contract from before this spec) — only its position in the call sequence
  moves, not its output.

## Error cases

- EC-1. Given the dropped source's directory is *also* gone from disk (not
  just removed from `config.yaml`), `resolve_mirror_location()` raising
  `PathNotWatchedError` for that location is caught and skipped (logged),
  not propagated — `init()` must not crash because one stale location's
  mirror can no longer be resolved.

## Contracts

```python
# vcs/services/versioning.py — new
def reconcile_dropped_sources(
    db_handler: DBHandler,
    dropped_locations: list[str],
    old_watch_targets: list[str],
) -> None: ...

# vcs/initialize.py — Initializer.init(), reordered, same public shape
def init(self) -> None: ...
```

No change to `sync_source_status()`'s own signature or SQL — it still does
exactly what it does today; `Initializer.init()` just captures the
before/after active-location sets around the existing call instead of
changing it internally.

## Non-goals / open questions

- Whether `reconcile_dropped_sources` belongs in `versioning.py` or
  `configure.py` — placed in `versioning.py` since it calls `git_store`
  directly, matching where `deleted_handle`/`created_handle` already live.
- The directory-also-gone degraded case (EC-1) is intentionally
  best-effort, not a full fix — see Scope/Out above.
