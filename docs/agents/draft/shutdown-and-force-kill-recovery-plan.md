# Draft — graceful SIGTERM (confirmed working) + force-kill recovery (gap, plan)

Status: draft, not started. Two separate findings from reading the actual
shutdown/write code, not assumed.

## Part 1 — graceful SIGTERM already drains pending work. No gap.

Traced the real call chain instead of assuming:

`daemon.py stop()` sends `SIGTERM` (POSIX) / `CTRL_BREAK_EVENT` (Windows,
mapped to `SIGBREAK`) → `VCSRuntime._handle_signal` sets `stop_event` →
`LocalRuntime.run()`'s poll loop exits → `VCSRuntime.stop()` →
`LocalRuntime.stop()` (`local_runtime.py:132-140`):

```python
def stop(self):
    self.stop_event.set()
    self.watcher.stop()      # stops the watchdog Observer, joins it
    self.bus.close()         # publishes STOP onto every subscriber queue
    self.consumer_worker.join()
    self.config_consumer_worker.join()
```

- `self.watcher.stop()` (`local_watcher.py:100-103`) calls
  `observer.stop()` + `observer.join()` - blocks until watchdog's dispatch
  thread actually terminates, so any filesystem event it was mid-dispatch
  on finishes being handed to the bus before this returns. No new events
  are watched for after this, but nothing already in flight is dropped.
- `self.bus.close()` (`bus.py:141-152`) publishes `STOP` onto each
  subscription's queue - a plain FIFO `queue.Queue`
  (`local_queue.py:8-33`). `STOP` lands **after** every event already
  queued, never ahead of it.
- Each consumer thread's loop (`consumer_worker.py:48-63`) is a plain
  `while True: event = self.queue.consume(); if event is STOP: break`.
  Because `STOP` is FIFO-behind real events, the thread drains every
  already-queued event through `self.consumer.handle(event)` **before**
  it ever sees `STOP`.
- `.join()` on both worker threads blocks until that drain (and the
  eventual `STOP`) actually completes - `stop()` does not return, and the
  process does not exit, until every already-queued event has been
  processed.

Net: a graceful `SIGTERM`/`ctx daemon stop` **does** finish everything
already queued before the process exits, exactly what was asked for. No
code change needed here - this section exists so the claim is backed by
the actual call chain, not just a rerun of the same assumption.

## Part 2 — force-kill: real gap, no recovery exists yet

A force-kill (`SIGKILL`, `taskkill /F` - `daemon.py`'s own `_force_kill`
fallback after the graceful-stop timeout, or an external `kill -9`) skips
all of the above. Two things were checked for damage; one is fine, one
isn't.

**Already safe, not an issue:**
- **SQLite** (`vcs/db/sqlite.py`, raw `sqlite3`): crash mid-transaction is
  exactly what SQLite's own rollback journal/WAL exists to survive - the
  next `open()` recovers automatically. Nothing app-level to add.
- **The cross-process mirror lock** (`git_store._lock_for`,
  `filelock.FileLock`, spec 012): a real OS-level lock (`flock` on POSIX,
  a byte-range lock on Windows) - the kernel releases it the instant the
  holding process dies, force-killed or not. Unlike a hand-rolled
  pid-file lock, there's no stale-lock cleanup to write.
- **`data/ctx.pid`**: `daemon.py start()` already checks
  `_is_process_alive(pid)` before trusting a stale PID file
  (`daemon.py:91-99`) - a force-killed daemon's stale PID is handled on
  the next `ctx daemon start` already.
- **`pending_actor_hints`**: TTL'd by design (spec 013) - a hint stranded
  mid-flight by a force-kill just expires, same as any other abandoned
  hint.
- **Missed/dropped content and scope changes while down**: `local_processing()`
  unconditionally re-writes every currently-configured source's current
  content on every boot (`local_adapter.py:53-61`, idempotent via
  `write()`'s no-op-on-identical-content check) and, as of
  [spec 026](../../specs/026-startup-scope-departure-reconcile.md),
  a source dropped while offline gets an honest removal commit too. Both
  run regardless of whether the previous shutdown was graceful or a
  force-kill - already covers "the daemon was down and reality drifted."

**The real gap - a stale git index from an interrupted plumbing write:**

`write()`/`remove()`/`move()` (`git_store.py:186-301`, spec 025) each run
several separate `git` subprocess calls under one lock -
`update-index`/`git rm --cached` (stages the change into the index) then
`write-tree` + `commit-tree` + `update-ref` (turns the staged index into an
actual commit). A force-kill landing **between** the index update and the
commit leaves the repo's index holding a staged-but-never-committed
change, with `HEAD` unchanged - the write looks like it silently never
happened, except the index disagrees with `HEAD` now.

That's not just "one write lost" (acceptable - the source file still has
the real content, the next real event re-derives it). It's a **latent
corruption of the next write to land in that same repo**: `write()`
always does `git write-tree` against the *entire current index*, not just
the path it's touching. The next legitimate write to any path in that
mirror repo builds its tree from an index that still carries the crashed
write's stale staged entry, silently mixing unrelated, never-actually-
committed content into a commit that looks completely normal - no error,
no warning, wrong tree. This is a pre-existing risk, not something spec
025 introduced (the old porcelain `git add` + `git commit` had the exact
same two-step gap), just never looked at directly before.

No self-heal exists for this today - confirmed by reading every
`git_store.py` function; nothing resets or verifies the index against
`HEAD` anywhere.

### Proposed fix: reset every mirror repo's index to HEAD at boot

The lock is the key fact that makes this safe: every real `write()`/
`remove()`/`move()` call holds `_lock_for(repo_path)` for its *entire*
stage-through-commit sequence (`git_store.py:195`, `:254`, `:285`) - so if
the lock is free, no write is legitimately in progress, and any index
entry that doesn't match `HEAD`'s tree can only be leftover from a crash.
That means a **boot-time reset is unconditionally safe**, not a heuristic:

```python
# sketch, not final - exact placement/naming a spec-time decision
def _reset_stale_index(repo_path: Path) -> None:
    with _lock_for(repo_path):
        head = _rev_parse_or_none(repo_path, "HEAD")
        if head is not None:
            subprocess.run(
                ["git", "read-tree", head],
                cwd=str(repo_path), check=True, capture_output=True,
            )
        # head is None (unborn HEAD, nothing ever committed) - index has
        # nothing legitimate to discard either; leave it as-is.
```

`git read-tree <tree-ish>` replaces the index wholesale with that
tree's/commit's entries and touches nothing else (no working tree
involved - safe on both bare and pre-existing non-bare repos, spec 025's
AC-9 compatibility applies here too). Cheap and idempotent when there was
nothing to fix (index already matches `HEAD` - the overwhelmingly common
case, a graceful prior shutdown or a repo that was never touched this
run), so running it unconditionally for every existing mirror repo at
`Initializer.init()` time - not per `init_repo()` call, which runs far
too often and would add pointless subprocess overhead - is the natural
home, consistent with spec 026's "reconcile once at boot" shape.

## Open questions (spec-time, not decided here)

- Where to enumerate "every existing mirror repo" from -
  `GIT_REPO_DIR`'s immediate subdirectories that pass `_is_initialized()`,
  most likely; no such enumeration helper exists in `mirror_path.py`
  today, would be new.
- Whether this belongs in `git_store.py` (co-located with the write
  functions whose failure mode it repairs) or `versioning.py`/
  `initialize.py` (co-located with spec 026's own boot-time reconcile,
  which this is thematically identical to - "self-heal at start").
- Whether to log/count how many repos actually needed a reset, as a
  signal that force-kills are happening in practice - not required for
  correctness, just an observability nice-to-have.

## Not done here

- No code changed. No spec written. Scoping only, per the "spec gets
  written only once someone is about to implement it" rule.
