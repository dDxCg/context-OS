# Implementation plan — directory event handling

Status: **planned, not started.** Fixes [issues.md](issues.md) #21 and #22.

## Context

Directory changes are not tracked. `locations` only ever holds **file** rows, and both
handlers match a single exact path, so a directory event updates nothing:

- **`deleted_handle`** (`src/vcs/services/versioning.py:93`) runs
  `UPDATE locations SET status = 0 WHERE location = ?`. A directory has no row, so
  deleting a folder leaves every child row at `status = 1` — the DB claims files exist
  that are gone.
- **`moved_handle`** (`src/vcs/services/versioning.py:75`) looks up
  `_get_context_id_by_location(db, event.dst)`, which stats the *directory*, finds no row,
  and so runs `WHERE context_id = NULL` — matching nothing. A silent no-op.

`is_dir` is set by `formatter.normalize_event` but **read nowhere** in the consumer or
versioning path — confirmed by grep across `src/`.

### The platform split that decides the design

Read from the vendored watchdog sources. **This is source-derived, not observed** — see
step 1 of Verification.

| | Windows (`read_directory_changes.py`) | Linux (`inotify.py`) |
|---|---|---|
| dir **delete** | one `FileDeletedEvent` carrying the *directory* path, `is_dir=False`, **no child events** | `DirDeletedEvent` + a `FileDeletedEvent` per child |
| dir **move** | `DirMovedEvent` + synthetic per-child `FileMovedEvent` (recursive watches) | same |
| dir **create** | `DirCreatedEvent` + synthetic per-child `FileCreatedEvent` | same |

Windows emits `FileDeletedEvent` unconditionally — it *cannot* check `isdir`, because the
path is already gone when the event is produced.

Three consequences:

1. **`is_dir` is untrustworthy for deletes.** Branching on it would fix Linux and leave
   Windows broken — and no unit test would catch it, since they all mock the watcher.
2. **Deletes are the real gap.** Linux self-repairs via per-child events; Windows loses the
   whole subtree.
3. **Moves work incidentally today** via synthetic child events — but only on recursive
   watches, at O(n) syscalls, racing an `os.walk` of the destination.

**Create genuinely needs no handler.** Children self-report via
`generate_sub_created_events`, and `normalize_event` already returns `None` for
`DirCreatedEvent`. Matches the intended design: *created → no action*.

---

## 1. `deleted_handle` — subtree-aware, no branching

Deactivate the exact path **and** everything beneath it in one statement. For a real file
the subtree clause matches nothing; for a directory it catches every child. Correct on both
platforms without ever asking whether the path was a directory.

```sql
UPDATE locations SET status = 0
 WHERE location = :path
    OR substr(location, 1, :n) = :prefix
```

with `prefix = path.rstrip("/") + "/"` and `n = len(prefix)`.

**Use `substr`, not `LIKE`.** `LIKE` treats `_` and `%` as wildcards, and `_` is common in
directory names — `LIKE '/my_dir/%'` would also match `/myXdir/...`. `substr` needs no
`ESCAPE` clause and cannot be got wrong.

The trailing `/` is load-bearing: without it, prefix-matching `/root` would also match
`/rootbeer`. The seeded fixture data (`/root`, `/root/a`, `/root/b`, `/other`) is a good
base; add a `/rootx` row to pin the false-match case.

## 2. `moved_handle` — subtree prefix rewrite + scope-aware status

Order matters: **do the subtree rewrite first** (pure SQL, no filesystem access), then
attempt the exact-node update. Today `moved_handle` calls `get_path_stats(event.dst)` up
front, which raises `FileNotFoundError` if `dst` has already moved or been deleted again;
doing SQL first makes the directory case immune to that.

```sql
-- children
UPDATE locations
   SET location = :dst_prefix || substr(location, :cut),
       status   = :active
 WHERE substr(location, 1, :n) = :src_prefix
```

`cut = len(src_prefix) + 1` (SQLite `substr` is 1-indexed): for `location='/root/a'` and
`src_prefix='/root/'`, `substr(location, 7)` = `'a'` → `/new/` + `a`.

`st_ino` / `st_dev` are **not** touched — a rename does not change them, and writing the
*directory's* inode onto child rows would corrupt identity.

Then keep the existing exact-node update (by inode → `context_id`) for the file-rename
case, guarded so a missing `dst` degrades instead of raising, and extended to set `status`.

**Status:** `active = 1 if is_path_in_scope(event.dst) else 0`. A directory renamed out of
every source keeps its paths accurate but is marked inactive, so running state matches what
`sync_source_status` would compute at the next restart.

`is_path_in_scope` comes from `vcs/services/configure.py`; `configure` does not import
`versioning`, so there is no import cycle. Cost is one YAML parse per move event, which is
rare.

## 3. Path-form precondition — do **not** normalize inside the handlers

Prefix matching only works if event paths and stored `location` values share one separator
style. They do: `normalize_event` runs every path through `path_normalize` (posix), and
stored locations come from `path_normalize` / `collect_files`.

Do **not** call `path_normalize` inside the handlers to "be safe". On Windows
`path_normalize("/root")` resolves to `C:/root`, which would break the existing
`test_deleted_handle_marks_location_inactive` fixture data and silently rewrite
already-correct paths. Document the precondition instead.

## 4. Small defensive fix — `local_consumer.py`

`LocalConsumer.handle` calls `modified_handle(..., tmp_file=TempFile.from_path(event.src))`
for any `ModifiedEvent`; on a directory that raises inside `read_file`. Unreachable today
(`normalize_event` returns `None` for `DirModifiedEvent`, which Linux emits constantly),
but one `if event.is_dir: return` guard on the modified branch is cheap insurance against a
future producer.

---

## Tests

`tests/unit/vcs/services/test_versioning.py`, using the existing `db_handler` + `seeder`
fixtures:

- delete a directory → all child rows go `status = 0`; unrelated rows (`/other`) untouched.
- delete a **file** → only that row changes (the subtree clause must no-op).
- prefix false-match: seed `/rootx`, delete `/root`, assert `/rootx` is untouched.
- move a directory → child `location` values rewritten under the new prefix, `st_ino` and
  `st_dev` unchanged, `/other` untouched.
- move a directory whose `dst` is **out of scope** → paths rewritten *and* `status = 0`
  (needs the `config_path` fixture).
- move a directory whose `dst` **is** in scope → `status = 1`.
- move where `dst` no longer exists on disk → subtree rewrite still applied, no exception.
- existing `test_moved_handle_updates_location_by_matching_inode` must still pass; it will
  need the `config_path` fixture added so the new scope check does not read the repo's real
  `config.yaml`. This is a real, if small, test-hermeticity change — do not let it pass by
  accident against the live config.

## Verification

```powershell
uv run pytest -q      # 158 passing today, plus the new cases
uv run ruff check .
```

Unit tests mock the watcher and **cannot** see any of the platform behaviour above, so the
decisive check is a live harness (extend the scratchpad set, e.g. `dir_events_check.py`)
running the real `LocalRuntime` against a real watchdog observer:

1. **First, empirically confirm the event table above** — log every normalized event while
   deleting and renaming a directory. The whole design rests on Windows emitting a single
   `FileDeletedEvent` with `is_dir=False` and no child events; that was read from source,
   not observed. If it differs, revisit §1 before building on it.
2. Create `src/dir_a/{one.txt,two.txt}`, let them be versioned, then **rename** `dir_a` →
   `dir_b`: both child rows must point under `dir_b`, keep `status = 1`, and retain their
   original `st_ino` / `st_dev`.
3. **Delete** `dir_b`: both child rows must go `status = 0`.
4. Rename a directory to a path **outside** every source: rows rewritten, `status = 0`.
5. Confirm no duplicate-work damage: watchdog also emits synthetic per-child move events,
   which will re-run `moved_handle` per file after the subtree rewrite. That should be
   idempotent — verify row counts and version counts do not grow.
6. Re-run `live_test.py` — must stay **16/16**.

Run the harness on Linux too (or lean on CI), since the delete path differs there.

## Known limitations — flagged, not fixed here

- **Renaming or deleting a watched source directory kills its watch.** On Windows
  `is_removed_self` calls `emitter.stop()`; after a rename the watch points at a dead path.
  Watches are only reconciled on config change, so the runtime keeps watching nothing until
  the config is edited or the process restarts. Arguably a config-level problem — the
  source list still names the old path — so it is deliberately out of scope.
- **No index on `locations.location`**, so prefix matching is a full table scan. Fine at
  current scale; worth an index if the table grows.
- **Existing rows are not repaired.** Any subtree already orphaned by a directory delete or
  rename stays wrong until a restart runs `sync_source_status`. These are forward-looking
  fixes only.
