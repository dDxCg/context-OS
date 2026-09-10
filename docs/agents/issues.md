# Known bugs — config hot-reload refactor

Found during code review of the worker-abstraction refactor (`3630dc5`) plus the
uncommitted fix-in-progress on top of it. Ranked most severe first.

Fixed: #1-#14 (each with a regression test that's no longer `xfail`, except
#9 — found and fixed via live-testing), #15/#19/#20/#16 (specs 014-017,
Tier 3), #21/#22 (fixed by the git-backend migration, `dec14a3`, but not
cross-referenced back here until a 2026-09-09 re-audit), #24
(test-hermeticity gaps found via a failing CI run, 2026-09-09), and
#25/#27/#28 (specs 022/023/024, all found live via
[scripts/live_integration_test.py](../../scripts/live_integration_test.py)
2026-09-09, all re-verified live after fixing). Moot: #17, #18 — described a
storage model the git migration replaced outright. **Open: #26** (`ctx
daemon` disappeared with no log trace, found live 2026-09-09) — #27's fix
(same day) is plausibly its root cause but this was not independently
re-confirmed, stays open pending a fresh live observation.

**#15-#20** were found while planning the config-control CLI and background
daemon — see [STATE.md](STATE.md). They are a different class from
everything above: rather than watcher or queue defects, they are gaps that
only become reachable once the CLI is installed as a real binary and the
runtime is stopped by a signal instead of Ctrl+C. #15, #17 and #18 each make
part of the planned CLI impossible, not merely degraded.

**#21-#22** are directory-tracking gaps — see
[STATE.md](STATE.md). Both are live data-correctness bugs
today, and #21 is **platform-dependent**: it is silently broken on Windows
and works incidentally on Linux, which is exactly the kind of split the unit
suite cannot see because it mocks the watcher.

#12-#14 came out of decoupling watch targets from config sources, so that
the MCP guardrail's file-granular grants stop creating one watcher per
approved file. All three were found by live-testing, not by unit tests —
the unit tests mock the watcher and so cannot see any of them.

#1 and #10 were fixed together via Option A (dedicated queue per consumer
plus a router), since they are mutually dependent: separating the config
consumer is pointless while no `Config*Event` is ever produced. That work
also surfaced #11 (watch path-format mismatch).

## 1. ~~Shared queue race causes indefinite shutdown block~~ — FIXED

**Files:** `src/vcs/workers/local/local_runtime.py:27`, `src/vcs/runtime.py:27-28`

`ConsumerWorker` and `ConfigConsumerWorker` are constructed with the *same*
`LocalQueue` instance:

```python
self.consumer_worker = ConsumerWorker(self.stop_event, queue=queue)
self.queue = queue or self.consumer_worker.queue
self.config_consumer_worker = ConfigConsumerWorker(self.stop_event, queue=self.queue, runtime=self)
```

Both threads block on `self.queue.consume()` (a plain `queue.Queue.get()`).
`stop()` publishes a single `STOP` sentinel, so only one of the two threads
ever dequeues it — the other blocks on `get()` forever. `LocalRuntime.stop()`
then hangs on whichever `.join()` corresponds to the thread that missed STOP.
This is the "config consumer worker thread blocking" issue named in commit
`3630dc5`.

The uncommitted fix (`runtime.py:28`, `self.queue.close()`) doesn't help:
it's placed *after* `self.local_runtime.stop()`, which is the call that hangs,
so it never executes. Even if it did, `LocalQueue.close()` only calls
`queue.task_done()`, which doesn't unblock a pending `get()`.

**Confirmed live (2026-07-19), and worse than originally scoped:** ran the
real `VCSRuntime` end-to-end (script kept at
`scratchpad/live_test_config_adapt.py` for reference, not checked in) and the
shared queue causes two distinct failures, not just the shutdown hang:

1. **Silent event loss in normal operation.** Creating a file in a watched
   source directory produced a `CreatedEvent` that got dequeued by
   `ConfigConsumerWorker` (log: `[SUCCEEDED] ConfigConsumer.handle`, not
   `LocalConsumer.handle`). `ConfigConsumer.handle()` only acts on `Config*`
   event types, so it silently no-ops on a plain `CreatedEvent` — the file
   was watched, hashed by nothing, and never versioned. Which of the two
   worker threads happens to win the race on `queue.get()` is nondeterministic,
   so this is a live, intermittent data-loss bug, not just a cosmetic
   shutdown issue.
2. **Shutdown hang, confirmed.** `stop()` did not return within an 8s bound;
   afterward `consumer_worker.is_alive() == False` (it got STOP) but
   `config_consumer_worker.is_alive() == True` (still blocked on `queue.get()`
   forever) — exactly as predicted.

**Fix direction — evaluated three options, recommending the third:**

- **A. Give each consumer its own dedicated queue.**
  `ConsumerWorker` keeps its own `LocalQueue`; `ConfigConsumerWorker` gets a
  *separate* one instead of `queue=self.queue`. Register the config file's
  own path as its own `watcher.add_watch(...)` entry with
  `callback=config_consumer_worker.queue.publish`, while source paths keep
  `callback=consumer_worker.queue.publish`. `normalize_event()` already
  decides `Config*Event` vs. plain `SourceEvent` per-path inside the
  `Handler.on_any_event` closure that `add_watch` creates, so once each
  watched path's callback points at the right queue, delivery separates for
  free — no dispatcher/router needed. `LocalRuntime.stop()` then publishes
  `STOP` to *both* queues before joining both threads. This is a small,
  local change reusing the `EventBroker`/`LocalQueue` abstraction that
  already exists, and it fixes both failure modes above (no more shared
  `.get()` race to lose events on, no more single-STOP shutdown race).
- **B. True pub/sub: one broadcast broker, every event fanned out to both
  consumers.** Each consumer still filters by `isinstance` (as `handle()`
  already does today) but now sees its own copy of every event via its own
  subscriber queue, so nothing is silently dropped; `STOP` gets fanned out
  the same way. This generalizes better if a third consumer type ever shows
  up (no producer-side change needed to add a subscriber), but requires a
  new broadcast `EventBroker` implementation and means every consumer does
  wasted `isinstance` work on events meant for the other one.
- **C. Single unified consumer, single queue (recommended).** Collapse
  `ConsumerWorker`+`LocalConsumer` and `ConfigConsumerWorker`+
  `ConfigConsumer` back into **one** worker thread reading **one**
  `LocalQueue`, with one dispatch entry point that branches on event type —
  same pattern `LocalConsumer.handle()` already uses for
  `Moved`/`Modified`/`Deleted`/`Created`, now also covering `Config*Event`.
  Handling a `Config*Event` does, in order: (1) recover/re-parse config for
  deleted/moved, as today; (2) refresh the permission/allowed-path *cache* —
  see the "MCP guardrail" note below, this is bookkeeping, not the
  authorization boundary; (3) re-run `sync_source_status`
  (`src/vcs/services/versioning.py`) so `locations.status` reflects the
  current source list, not just at startup — also allowed to lag; (4) diff
  sources (`get_config_diff`, unchanged), collect files for newly-added
  sources, and publish their `CreatedEvent`s back onto the *same* queue;
  (5) add/remove watches (issue #10, still a prerequisite either way).
  Because it's one queue and one consumer, there's no second reader to lose
  the race with — issue #1 becomes structurally impossible, not just less
  likely — and the bookkeeping above always happens-before the
  `CreatedEvent`s it triggers, since they're the same FIFO queue processed
  by the same thread. `LocalRuntime.stop()` goes back to one `STOP`
  sentinel and one `.join()`. Trade-off: no parallelism between
  config-adapt work and versioning work (one thread now does both,
  sequentially) — acceptable given both are fast, file-local operations.
- **MCP guardrail note:** the "update permission" step above is *not* what
  MCP access control ends up resting on. Resolved separately (full design
  in [FUTURE.md](FUTURE.md)): MCP tool calls check
  the requested path against **current** state synchronously at call time
  and block if out of scope — a hard rule, independent of whether this
  queue has processed the relevant `Config*Event` yet. DB/status sync is
  free to lag; the guardrail is what's not allowed to.
- **Recommendation: C, but no longer for the reason first given.** The
  ordering-guarantee argument above used to be why C beat A on
  *correctness*. Now that MCP enforcement is synchronous and separate from
  the queue, that argument doesn't apply — A and C are a throughput/latency
  trade-off, not a correctness one. Still recommending **C for now**: it's
  simpler (fewer moving parts, one `STOP`, no per-queue handling), there's
  no throughput pressure yet, and it's still the fix for the live-confirmed
  event-loss/shutdown-hang bug either way. Revisiting **A** later is not
  blocked by any correctness concern anymore — only an actual reason to
  (independent scaling, isolating a slow handler). Revisit **B** only if a
  third consumer type is ever added — neither A nor C's two-consumer-role
  assumption holds at that point.

**Implemented: Option A** (not C). With MCP enforcement synchronous, the
choice was a simplicity/throughput trade-off rather than a correctness one,
and A keeps config-adapt work off the versioning thread — the config
consumer now does inline DB writes (`collect_files` → `created_handle` /
`deleted_handle`) that would otherwise stall file versioning behind a
directory walk.

What changed:
- `LocalRuntime` owns two `LocalQueue`s (`queue`, `config_queue`) and
  publishes `STOP` to both in `stop()`.
- `LocalRuntime._route()` demuxes watcher events by type; the config-file
  watch uses `_route_config_only()`, which drops non-config events so
  unrelated siblings in the config's parent directory aren't versioned.
- `CONFIG_EVENTS` in `shared/types.py` is the single discriminator. It's
  needed because `ConfigCreatedEvent` subclasses `CreatedEvent`, so a bare
  `isinstance(e, CreatedEvent)` is true for config events. `LocalConsumer`
  now early-returns on it as defence in depth.
- `ConfigConsumer.handle()` conforms to the `Consumer` ABC again — the
  `runtime` parameter is gone (with it, the circular import and the
  reference cycle). It takes an injected `watcher` and `publish` callback,
  set via the new `ConsumerWorker._configure_consumer()` hook, which let
  `ConfigConsumerWorker` drop its duplicated `run()` loop entirely.
- New source watches now go through the router rather than
  `consumer_worker.consumer.handle`, which had been calling into a
  cross-thread sqlite connection from the watchdog dispatch thread.

Verified end-to-end: editing `config.yaml` adds/removes watches, ingests
new sources at `status=1`, flips removed sources to `status=0`, and
shutdown returns promptly with both workers joined.

**Update:** the direction is now to eventually move `EventBroker` to
RabbitMQ (multiple, possibly out-of-process producers — see the `3rd-party`
source type already stubbed in `config.example.yaml`). This doesn't
conflict with Option C above: the RabbitMQ *topology* (two queues,
`source.#`/`config.#`, bound from one exchange) is a broker-side routing
choice independent of how many local consumer threads read them — even a
single future RabbitMQ consumer could bind both patterns onto one queue,
mirroring C. See [FUTURE.md](FUTURE.md)
for the full topology and interim-step design, now updated to describe Option C.

## 2. ~~`Config*Event()` construction always raises `TypeError`~~ — FIXED

**Files:** `src/utils/formatter.py:12,16,20`, `src/vcs/shared/types.py:79-92`

```python
@dataclass
class ConfigCreatedEvent(CreatedEvent):
    src = path_normalize(get_config_path())   # no type annotation
```

Because `src` had no type annotation here, `@dataclass` did **not** treat it
as a field default — `src` stayed a required, no-default field inherited from
`SourceEvent`. `formatter.normalize_event()` then called `ConfigCreatedEvent()`,
`ConfigModifiedEvent()`, `ConfigDeletedEvent()` with zero arguments, which
raised `TypeError: __init__() missing 1 required positional argument: 'src'`.

This call happens inside `WatchWorker.add_watch`'s `Handler.on_any_event`,
*outside* the try/except that wraps the callback — so it was unhandled and
propagated in the watchdog dispatch thread on every config file change.

Fixed by giving `src` a real dataclass default:
`src: str = field(default_factory=lambda: path_normalize(get_config_path()))`
on all four `Config*Event` subclasses. `default_factory` also defers the
`get_config_path()` call to instantiation time instead of class-definition
time, avoiding a separate crash if `CONFIG_PATH` isn't set yet when
`vcs.shared.types` is imported. Covered by
`tests/unit/utils/test_formatter.py::test_normalize_event_maps_config_file_created`
(no longer `xfail`).

## 3. ~~`recover_config()` crashes: missing `()` on `get_config_path`~~ — FIXED

**File:** `src/vcs/services/configure.py:91`

```python
config_path = get_config_path   # should be get_config_path()
Path(config_path).parent.mkdir(...)
```

Assigned the function object instead of calling it, so `Path(config_path)`
raised `TypeError: argument should be a str or an os.PathLike object ...
not 'function'`. Triggered by `ConfigConsumer.handle()` whenever the watched
config file was deleted or renamed — recovery always crashed instead of
restoring from the snapshot.

Now calls `get_config_path()`. Covered by
`tests/unit/vcs/services/test_configure.py::test_recover_config_restores_file_from_snapshot`
(no longer `xfail`).

## 4. ~~`WatchWorker.remove_watch()` always raises `TypeError`~~ — FIXED

**File:** `src/vcs/workers/local/local_watcher.py:51`

```python
self.jobs = []                       # list of (path, callback) tuples
...
watch_token = self.jobs.pop(path, None)
```

`list.pop()` only accepts a single integer index — no key lookup, no default.
Calling it with `(path, None)` raised
`TypeError: pop() takes at most 1 argument (2 given)`. Triggered whenever a
source was removed from the config file.

Separately, `add_watch` never stored the `ObservedWatch` object returned by
`self.observer.schedule(...)` — `jobs` held `(path, callback)`, not the watch
handle `observer.unschedule()` needs.

Fixed by having `add_watch` append the watch handle too —
`(path, callback, watch)` — and having `remove_watch` linear-scan `jobs` by
path, removing the matching entry and calling `observer.unschedule()` on its
watch handle. Covered by
`tests/unit/vcs/workers/local/test_local_watcher.py::test_remove_watch_unregisters_a_previously_added_path`
(no longer `xfail`).

## 5. ~~`ConfigConsumer.handle()` passes a nonexistent `path` kwarg~~ — FIXED

**File:** `src/vcs/workers/config/config_consumer.py:32`

```python
event = CreatedEvent(path=f)
```

`CreatedEvent`/`SourceEvent`'s field is named `src`, not `path`. This raised
`TypeError: __init__() got an unexpected keyword argument 'path'` for every
file discovered under a newly added config source — the "watch a new source
added via config" path was non-functional.

Fixed to `CreatedEvent(src=f)` (also renamed the loop variable from `event`
to `created_event` since it was shadowing the outer `handle(self, event,
runtime)` parameter). Covered by
`tests/unit/vcs/workers/config/test_config_consumer.py::test_handle_config_modified_publishes_created_event_for_new_files`
(no longer `xfail`).

## 6. ~~`parse_config()` crashes on an empty config file~~ — FIXED

**File:** `src/vcs/services/configure.py:69`

`init_config_file()` creates a 0-byte file via `.touch()` when none exists.
`yaml.safe_load()` on empty content returns `None`, and the very next line
did `config_content["sources"]`, raising
`TypeError: 'NoneType' object is not subscriptable`. Hit on first run /
fresh config setup, via `Initializer.init() -> store_config_snapshot() ->
parse_config()`.

Fixed with `config_content = yaml.safe_load(f) or {"sources": []}`. The same
`None`-from-empty-file case in `Initializer._get_sources` (issue #7) got the
identical fix. Covered by
`tests/unit/vcs/services/test_configure.py::test_parse_config_on_freshly_initialized_file_does_not_crash`
(no longer `xfail`).

## 7. ~~`Initializer.__init__` reads the config file before it's created~~ — FIXED

**File:** `src/vcs/initialize.py:19`

```python
def __init__(self):
    self.db_handler = DBHandler.from_url(get_db_url())
    self.sources = self._get_sources(get_config_path())   # opens the file
```

`VCSRuntime.__init__` builds `Initializer()` — which opened the config path
immediately — before `VCSRuntime.run()` ever called `self.initializer.init()`
(the method that created the file via `init_config_file()` if missing). On a
genuinely fresh environment with no pre-existing config file, this raised
`FileNotFoundError` at construction time, before initialization logic got a
chance to run.

Fixed by calling `init_config_file()` in `__init__` before reading sources
(and dropping the now-redundant call from `init()`), plus guarding
`_get_sources` against an empty file the same way as issue #6:
`yaml.safe_load(f) or {"sources": []}`. Covered by
`tests/unit/vcs/test_initialize.py::test_initializer_can_be_constructed_before_config_file_exists`
(no longer `xfail`).

## 8. ~~`modified_handle()` never hashes the new content — reuses the previous version's hash~~ — FIXED

**File:** `src/vcs/services/versioning.py:54-68`

*Found while writing regression tests for this file — not part of the config
hot-reload refactor, pre-existing in the versioning core.*

```python
current_version = _check_current_version(db_handler, context_id)
new_hash = _get_version_hash(db_handler, context_id, current_version)   # fetches the CURRENT (old) version's hash
if _decide_to_append_version(tmp_file, content_hash=new_hash):
    version = Version(version_number=current_version+1, context_id=context_id, content_hash=new_hash)
    _append_version(db_handler, version, commit=True)
    tmp_file.move_tmp_file(BLOB_DIR / f"{new_hash}.blob")
```

Despite the name, `new_hash` was the *existing* version's `content_hash`
(`_get_version_hash` is queried with `version_number=current_version`, i.e.
the latest version already in the DB) — `modified_handle` never called
`gen_hash` on the new content anywhere. So every appended version was
recorded with the **previous** version's hash, and the new blob got moved to
`{old_hash}.blob`, overwriting the old blob's file with the new content under
the wrong name.

Fixed by renaming that variable to `previous_hash` (used only for the
similarity comparison in `_decide_to_append_version`) and computing a real
`new_hash = gen_hash(tmp_file.read_bytes())` for the version row and blob
filename. Covered by
`tests/unit/vcs/services/test_versioning.py::test_modified_handle_stores_new_content_under_its_own_hash`
(no longer `xfail`).

## 9. ~~`TempFile` fails to import outside pytest~~ — FIXED

**File:** `src/vcs/shared/temp_file.py:3`

*Found live-testing the app (`python -m vcs.runtime`) directly for the first
time in this review — every earlier fix had only been verified under pytest.*

```python
from src.utils.helper import save_to_file, gen_hash, make_dirs, path_normalize, read_file
```

Every other module in the codebase imports `from utils.helper import ...`
(no `src.` prefix) — `utils`, `vcs`, and `app` are the top-level importable
packages (the editable install's `.pth` file points `sys.path` at `src/`
itself, not its parent). This one file alone had a stray `src.` prefix, so
`from src.utils.helper import ...` raised `ModuleNotFoundError: No module
named 'src'` — but only outside pytest. `pytest.ini`'s `pythonpath = .` adds
the *repo root* to `sys.path` as well, which happens to make `src.utils...`
resolve too, masking the bug in every test run. Running the actual app
(`python -m vcs.runtime`, or the Docker entrypoint) crashed immediately on
import, before ever reaching `main()`.

Fixed to `from utils.helper import ...`, matching every other module.

## 10. ~~Config file itself is never watched — hot-reload is dead code today~~ — FIXED

**File:** `src/vcs/workers/local/local_runtime.py:37-43` (`_init_worker`)

*Found live-testing config hot-reload end-to-end — not previously covered
because all `ConfigConsumer`/`formatter` tests construct `Config*Event`s
directly rather than going through a real `watchdog` dispatch.*

```python
def _init_worker(self, sources):
    for source in sources:
        if source["type"] == "local":
            self.watcher.add_watch(
                source["path"],
                callback=self.consumer_worker.queue.publish
            )
```

Only the configured *source* paths ever get `watcher.add_watch(...)` called
on them. The config file itself (`CONFIG_PATH`, e.g. `config.yaml` at the
repo root) is never registered as a watch target anywhere — confirmed live:
after editing `config.yaml` to add a new source while the app was running,
`runtime.local_runtime.watcher.jobs` still only listed the original source
path, and no context rows appeared for the new source's file even after
waiting past the debounce window. `get_config_diff()` called manually
*did* correctly compute `{"added": [...], "deleted": []}` — the diffing
logic itself works — but nothing ever triggers it, because
`normalize_event()`'s `src == config_path` check inside
`Handler.on_any_event` can only ever fire for a directory that's actually
being watched, and the config file's directory (typically the repo root,
outside every configured source) isn't one of them.

So even after issue #1 is fixed, editing `config.yaml` while the app is
running will silently do nothing — `ConfigConsumer`/`ConfigConsumerWorker`
exist and are wired up correctly, but never receive an event to act on.

**Fixed** alongside #1. `_init_worker` now registers
`Path(get_config_path()).parent` with `recursive=False` and
`callback=self._route_config_only`.

Two details that matter:
- **`recursive=False` is mandatory.** The config file's parent is typically
  the repo root; a recursive watch there would flood the queue with events
  for the entire tree.
- **The callback must drop non-config events.** That watch still fires for
  unrelated siblings in the parent directory, and `normalize_event()`
  returns *plain* events for them. Routing those onto the local queue would
  version arbitrary repo-root files that no source covers — hence
  `_route_config_only()` rather than the general `_route()`.

Watching the parent rather than the file itself is deliberate: `watchdog`
is unreliable watching a single file, and editors commonly save via atomic
rename, which destroys a file-level watch.

Confirmed live: editing `config.yaml` under a running runtime now produces
a real `ConfigModifiedEvent` that reaches `ConfigConsumer.handle`.

## 11. Watch path-format mismatch made `remove_watch()` silently no-op — FIXED

**Files:** `src/vcs/workers/local/local_watcher.py` (`add_watch`/`remove_watch`)

*Found while live-testing the #1/#10 fix — the delete path appeared to work
in unit tests because they mock the watcher.*

`WatchWorker.remove_watch()` matches jobs by string equality
(`job[0] == path`), but the two callers registered and looked up paths in
different formats:

- `LocalRuntime._init_worker` watched `source["path"]` straight from
  `Initializer._get_sources()`, which is a raw `yaml.safe_load` — **not**
  normalized, so backslashed on Windows.
- `ConfigConsumer` removes paths from `get_config_diff()`, which goes
  through `parse_config()` → `path_normalize()` — posix separators.

So `C:\...\src_a` never equalled `C:/.../src_a`, and removing a source that
had been watched since startup silently did nothing. The watch stayed live:
files under a source removed from config kept being versioned, and their
`locations.status` would flip back to 1 on the next edit after the config
consumer set it to 0.

Fixed by normalizing in `WatchWorker` itself — `add_watch()` and
`remove_watch()` both call `path_normalize()`, so registration and lookup
agree regardless of what format the caller passes.

---

*Cross-referenced in [note.md](note/note.md) under "Known issues".*

## 12. File-granular sources are not watchable, and create one watcher each — FIXED

**Files:** `src/vcs/services/configure.py` (`derive_watch_targets`),
`src/vcs/workers/local/local_watcher.py` (`reconcile`),
`src/vcs/workers/local/local_runtime.py` (`_route`)

`config.yaml` sources served two roles at once: the access-control scope
list and the watch-target list. Those want opposite granularity. Scope
wants to be narrow — the MCP guardrail deliberately grants the exact file
(`add_sources([path])` in `app/mcp/guardrail.py`) so approving one file
doesn't expose its siblings. Watches want to be coarse — one directory
watch covers a whole tree.

Two consequences:

1. **A file path is not watchable.** The granted file path reached
   `observer.schedule(handler, <file>, recursive=True)`. Watchdog documents
   that parameter as *"Directory path that will be monitored"*, and the
   Windows backend calls `CreateFileW(path, FILE_LIST_DIRECTORY, …)` then
   `ReadDirectoryChangesW`, which fails on a non-directory handle. On Linux
   inotify it appears to work but breaks on the atomic-rename saves most
   editors perform.
2. **One watcher per approved file.** An agent reading 50 files in one
   directory produced 50 watches where 1 would do. No cap existed anywhere.

**Fixed** by making watch targets *derived* rather than 1:1 with sources:

- `derive_watch_targets(config=None)` maps each source to its containing
  directory, then drops any directory an ancestor already covers
  recursively. It walks up to the nearest *existing* directory, and returns
  nothing rather than falling back to a filesystem root — resolving a
  fully-missing path to `/` or `C:\` would put the whole disk under the
  watcher.
- `WatchWorker.reconcile(desired, callback, tag=...)` replaces a tagged
  watch set wholesale. Per-source add/remove deltas cannot express "these
  two sources collapsed into one watch". Jobs carry a `tag` so reconciling
  source watches never collects the config-file watch.
- `LocalRuntime._route` now filters events through `is_path_in_scope()`.
  This is load-bearing: a derived watch is deliberately *broader* than the
  granted scope, so without the filter, watching `/a` because `/a/x.txt`
  was approved would also version `/a/secret.txt`. Scope enforcement moved
  from "only watch what's in scope" to "filter what we watch against scope".
- The parsed config is memoized in the router, since `is_path_in_scope`
  re-parses `config.yaml` on every call and now runs per filesystem event.
  No lock: watchdog's `BaseObserver.dispatch_events` runs every handler on
  a single dispatcher thread. That dependency is commented in the code.
- `add_sources` now skips paths already in scope, not just exact
  duplicates, so `config.yaml` stops accumulating subsumed entries.

Verified live: two files approved in the same new directory grow the watch
set by exactly one entry (the parent directory), both get `status=1`, and
an unapproved sibling in that same directory is not versioned.

## 13. Debounce silently drops config changes — FIXED

**File:** `src/vcs/workers/local/local_watcher.py` (`add_watch`)

`WatchWorker._should_process` coalesces events per path within 0.5s. For
ordinary files a dropped event is harmless — the next edit re-fires it.
For the config file it is **permanent**: the dropped event carried a diff
that is never re-applied.

Rapid successive writes are the *normal* case for `config.yaml`, because
the guardrail calls `add_sources()` once per approved path — an agent
reading several files in a row writes the config several times well inside
the debounce window.

**Fixed** with a `debounce=True` parameter on `add_watch`; `LocalRuntime`
passes `debounce=False` for the config watch. Processing a config event
twice is harmless (the second diff is empty), so opting out is safe.

## 14. Config diff and snapshot read the file separately — FIXED

**Files:** `src/vcs/services/configure.py` (`get_config_diff`,
`store_config_snapshot`), `src/vcs/workers/config/config_consumer.py`

The worst of the three, and invisible until #13 was fixed. `get_config_diff()`
and `store_config_snapshot()` each independently re-read `config.yaml`. If
the file changed between those two reads, the snapshot advanced to state
that was **never applied** — and because the baseline had moved, every
later diff reported nothing. The change was lost permanently.

Observed live with two rapid approvals: `config.yaml` correctly contained
both `one.txt` and `two.txt`, `one.txt` was ingested, and `two.txt` had no
`locations` row and never got one — subsequent diffs were all empty.

**Fixed** by reading the config once and threading that object through the
whole operation. `get_config_diff(config=...)` and
`store_config_snapshot(config_content=...)` both accept it, and
`ConfigConsumer.handle` calls `parse_config()` a single time and passes the
result to the diff, the watch derivation, and the snapshot. The baseline
now only ever advances to state that was actually applied, so a concurrent
write is picked up by the next event instead of being swallowed.

---

## 15. ~~Wheel packaging omits `src/utils` — the installed CLI cannot start~~ — FIXED

**File:** `pyproject.toml:38-39`

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/app", "src/vcs"]
```

`utils` is a third top-level package (confirmed by
`src/chrono_ctx.egg-info/top_level.txt`, which lists `app`, `utils`, `vcs`) and it is
imported unconditionally on every startup path:

- `vcs/runtime.py` → `from utils.logger import setup_logger`
- `vcs/initialize.py` → `from utils.helper import get_db_url, get_config_path`
- `vcs/services/configure.py` → `from utils.helper import ...`
- `vcs/shared/config.py` → `from utils.helper import anchored`

A built wheel therefore installs the `ctx` console script and dies with
`ImportError: No module named 'utils'` on the first command. This is invisible today only
because the venv carries an editable install (`_editable_impl_chrono_ctx.pth`) that maps
the real source tree.

Note the `egg-info` directory is a **stale setuptools artifact** — the project moved to
hatchling in `cbad045` and `*.egg-info` is gitignored. Its `top_level.txt` happens to be
correct where `pyproject.toml` is wrong, but do not trust the rest of it: `SOURCES.txt`
still references `vcs/workers/3rd_party/polling_worker.py`, which no longer exists.

**Fix:** add `"src/utils"` to the wheel packages list. Verify with a real build installed
into a clean venv, not with the editable install.

**Fixed:** [014-wheel-packaging-utils.md](../specs/014-wheel-packaging-utils.md).
`pyproject.toml`'s wheel packages list now includes `"src/utils"`. No `build`/`installer`
dependency exists in this project to verify an actual install in CI, so the regression test
pins the packages list itself — the real defect surface.

## 16. ~~No SIGTERM handler — every non-Ctrl+C shutdown skips cleanup~~ — FIXED

**Files:** `src/vcs/runtime.py:19-23`, `src/vcs/workers/local/local_runtime.py:124-127`

The only stop trigger anywhere in the codebase is `except KeyboardInterrupt`. There is no
`signal.signal(...)` call in `src/` at all.

SIGTERM does not raise `KeyboardInterrupt` — Python's default handler terminates the
process immediately. So on `docker stop`, `systemctl stop`, or any supervisor-issued
shutdown, `VCSRuntime.stop()` never runs: the watcher is not stopped, the queues are never
drained of their `STOP` sentinels, worker threads are not joined, and the SQLite connection
is never closed (`DBHandler.close()` is called on no shutdown path at all).

This is currently latent because the documented way to run the app is a foreground
`python -m vcs.runtime` ended with Ctrl+C. It becomes a live defect the moment the runtime
is backgrounded, which is exactly what `ctx daemon` (spec 019, see
[STATE.md](STATE.md)) adds.

**Fix:** install `SIGTERM`/`SIGINT` handlers that call `VCSRuntime.stop()`, and make
`stop()` idempotent — `LocalRuntime` already catches `KeyboardInterrupt` and calls its own
`stop()`, so the outer handler can currently double-stop and re-publish `STOP` onto a
closed queue.

**Windows caveat that shapes the daemon design:** `os.kill(pid, SIGTERM)` on Windows maps
to `TerminateProcess` — abrupt, no cleanup, defeating this fix entirely. A stopper must
send `CTRL_BREAK_EVENT` (which requires the child to have been spawned with
`CREATE_NEW_PROCESS_GROUP`); that raises `KeyboardInterrupt` in the child, reusing the path
the runtime already handles.

**Fixed:** [017-sigterm-handler.md](../specs/017-sigterm-handler.md). `VCSRuntime.run()`
installs a `SIGTERM` handler that sets `stop_event` (unblocking `LocalRuntime.run()`'s
polling loop), and `stop()` runs unconditionally after `local_runtime.run()` returns —
not just on `KeyboardInterrupt` — and is now idempotent, since it's reachable both from a
signal and a subsequent Ctrl+C. The Windows `CTRL_BREAK_EVENT` side (what an external
stopper sends) stays out of scope — that's a future process-supervision concern, not
something `VCSRuntime` itself does.

## 17. ~~`created_handle` never writes a blob — v1 content is unrecoverable~~ — MOOT

**Files:** `src/vcs/services/versioning.py:11-51` (`_append_context`),
`src/vcs/adapters/local_adapter.py:31-32`

Two ingest paths disagree about persisting content:

- `LocalAdapter.local_file_processing` — the **startup** path — writes the blob:
  ```python
  _append_context(self.db_handler, context_entry)
  save_path = BLOB_DIR / f"{content_hash}.blob"
  save_path.write_bytes(file_content)
  ```
- `created_handle` → `_append_context` — the **watcher** path — inserts the `versions` row
  with its `content_hash` but never writes the corresponding blob. Only `modified_handle`
  does, at `versioning.py:69`.

So a file present at startup has a retrievable v1, but a file created *while the daemon is
running* does not — including every file added through MCP guardrail approval, since
`ConfigConsumer` calls `created_handle` directly.

This is the same startup-vs-hot-path divergence class as issues #10 and #12.

**Verified against `data/db-dev.sqlite`, not inferred:**

```
version rows: 17    blob files: 9
versions WITHOUT a blob: 4      <- content permanently unrecoverable
```

**Consequence:** `ctx rollback <file> -v 1` can never work for a watcher-created file, and
`ctx diff` involving such a version has nothing to read. This makes the planned audit
commands impossible, not merely degraded.

**Fix:** have `_append_context` persist the blob when it creates a version, matching
`LocalAdapter`. Forward-looking only — the 4 existing blob-less versions cannot be
recovered, which is why `get_version_list` is specced to expose an `available` flag per
version.

**Re-audited 2026-09-09, moot.** `dec14a3` ("migrate db + service to use git") replaced the
separate `BLOB_DIR`/content-hash storage model entirely — `_append_context`
(`versioning.py:56-61`) now writes content straight into the git mirror repo via
`git_store.write`, which by construction always creates a real git blob in the same commit
that records the version. There is no longer a code path that records a version row without
persisting content. See [004-versioning-write-path-on-git.md](../specs/004-versioning-write-path-on-git.md).

## 18. ~~`modified_handle` has no same-hash guard — every version is duplicated~~ — MOOT

**File:** `src/vcs/services/versioning.py:54-71`

`_append_context` guards against re-recording identical content via
`_check_existed_version` (`versioning.py:23`). `modified_handle` has no equivalent check.

**Verified against `data/db-dev.sqlite`** — the only file in the database with real history
is entirely duplicate pairs:

```
01KW7DM9KX5ZPCHN8M8H8T5VAD   github-cicd.yaml
  v1  0a0a6a4737  MISSING       v2  0a0a6a4737  MISSING
  v3  6e113b86de  blob          v4  6e113b86de  blob
  v5  c44b6f2ef8  MISSING       v6  c44b6f2ef8  MISSING
```

Three distinct contents stored as six versions — a 100% duplication rate. `ctx history`
would show doubled noise and `ctx diff --from 1 --to 2` would report no difference.

**Root cause is only partly established.** The v1/v2 pair follows from issue #17: with no
blob on disk, `_decide_to_append_version` hits
`if not current_blob_path.exists(): return True` and forces a new version even though the
content is unchanged. The v3/v4 pair is **not** explained by static reading — the blob for
v3 does exist, so the similarity check should have returned `False`. Do not assume the two
pairs share a cause; the missing guard is the correct fix either way, but the v3/v4
mechanism warrants confirmation during implementation.

**Fix:** reuse `_check_existed_version` in `modified_handle`, exactly as `_append_context`
already does. Forward-looking only; existing duplicate rows stay.

**Re-audited 2026-09-09, moot.** Same migration replaced the content-hash-equality guard
with `_should_commit()` (`versioning.py:88-94`): a text-similarity check
(`NEW_VERSION_THRESHOLD`) against the current file in the mirror repo, run before every
`modified_handle` write (`versioning.py:76`). Unchanged/near-duplicate content no longer
produces a new commit. See
[004-versioning-write-path-on-git.md](../specs/004-versioning-write-path-on-git.md).

## 19. ~~`TempFile.TMP_DIR` is cwd-relative~~ — FIXED

**File:** `src/vcs/shared/temp_file.py:6`

```python
class TempFile:
    TMP_DIR = Path("data/tmp")
```

Every other configured path in the project was moved to project-root anchoring precisely
because "the MCP server and the VCS runtime are separate processes whose working
directories need not match" (`vcs/shared/config.py:6-9`). This constant was missed.

Harmless while the runtime is launched from the repo root; breaks for a detached daemon,
which has an arbitrary cwd — it would scatter `data/tmp` directories wherever it happened
to start, and `modified_handle` would stage blobs outside the real data directory.

**Fix:** route through `anchored()` like `SNAPSHOT_DIR`. Note it is a class attribute
evaluated at import, so tests must monkeypatch the attribute rather than an env var (the
same reason `tests/fixtures/config.py` patches `configure.CONFIG_SNAPSHOT_FILE` directly).

**Fixed:** [015-temp-file-anchored-path.md](../specs/015-temp-file-anchored-path.md).
`TMP_DIR` is now `Path(anchored(os.getenv("TMP_DIR", "data/tmp")))`, matching
`SNAPSHOT_DIR`'s pattern exactly, env-overridable the same way.

## 20. ~~No WAL mode and no busy timeout — CLI and daemon will contend~~ — FIXED

**File:** `src/vcs/db/sqlite.py:10-12`

```python
@classmethod
def from_url(cls, db_url):
    return cls(sqlite3.connect(db_url))
```

No `timeout` argument (so the default busy timeout applies with no retry anywhere), and
`PRAGMA journal_mode=WAL` is set nowhere — not in `sqlite.py`, not in `services/db.py`, not
in `data/schema.sql`. In SQLite's default rollback-journal mode a writer blocks all readers.

Not a problem today, because only one process ever opens the database. It becomes one as
soon as a CLI reads the DB while the daemon is writing: `ctx history` can fail with
`database is locked`, and `ctx rollback` — a write — can fail against a busy daemon.

Related sharp edges in the same file: `execute()` does not guard `self.conn is None` (giving
`AttributeError: 'NoneType' object has no attribute 'cursor'` after `close()`, where
`execute_script` raises a clear `ValueError`), and `execute()` defaults to `commit=True` so
even reads commit.

**Fix:** enable WAL and pass a `timeout` in `from_url`; add `__enter__`/`__exit__` to
`DBHandler` for short-lived CLI use — it already has `close`/`commit`/`rollback`/`begin`.
That also fixes the runtime's connection never being closed (issue #16).

**Fixed:** [016-sqlite-wal-timeout.md](../specs/016-sqlite-wal-timeout.md). `from_url` now
sets `PRAGMA journal_mode=WAL` and passes an explicit `timeout` (default 30s, overridable),
and `DBHandler` supports `with DBHandler.from_url(...) as db:`. The "related sharp edges"
(`execute()`'s missing `None`-guard, its `commit=True` default) were left untouched — a
separate behavior change each, not part of this fix.

## 21. ~~Deleting a directory leaves every child row active~~ — FIXED

**File:** `src/vcs/services/versioning.py:93` (`deleted_handle`)

```python
query = Query(
    query = "UPDATE locations SET status = 0 WHERE location = ?",
    params = (event.src,)
)
```

`locations` only ever holds **file** rows — directories are never inserted. So an exact
path match against a directory updates nothing, and every file underneath keeps
`status = 1`. **The database claims files exist that are gone**, and stays wrong until the
next restart, when `sync_source_status` reconciles by stat.

**This is platform-dependent, and worse on Windows.** From the vendored watchdog sources:

| | Windows (`read_directory_changes.py`) | Linux (`inotify.py`) |
|---|---|---|
| dir delete | one `FileDeletedEvent` carrying the *directory* path, `is_dir=False`, **no child events** | `DirDeletedEvent` + a `FileDeletedEvent` per child |

Windows [line 96-97](../.venv/Lib/site-packages/watchdog/observers/read_directory_changes.py)
emits `FileDeletedEvent` unconditionally — it *cannot* check `isdir`, because the path is
already gone by the time the event is produced. Linux therefore repairs itself incidentally
via per-child events; Windows loses the entire subtree.

Two consequences worth stating plainly:

- **`is_dir` cannot be trusted for deletes.** It is `False` for a deleted directory on
  Windows. A fix that branches on `is_dir` would repair Linux and leave Windows broken.
  (`is_dir` is set by `formatter.normalize_event` but read *nowhere* in the consumer or
  versioning path today — confirmed by grep across `src/`.)
- No unit test can catch this. The suite mocks the watcher, so the platform split is
  invisible to it.

**Fix direction:** make the delete subtree-aware unconditionally, so it never has to ask
whether the path was a directory — one statement deactivating the exact path *and*
everything beneath it. For a real file the subtree clause matches nothing. Use `substr`
rather than `LIKE`: `LIKE` treats `_` as a single-character wildcard and underscores are
common in directory names, so `LIKE '/my_dir/%'` would also match `/myXdir/...`. (This
SQL-prefix design was superseded before being built — see
[STATE.md](STATE.md)
for what actually fixed this.)

**Re-audited 2026-09-09, confirmed fixed.** `dec14a3` implemented exactly the fix direction
above: `deleted_handle` (`versioning.py:178-185`) now runs one `UPDATE ... WHERE location = ?
OR substr(location, 1, ?) = ?`, unconditional on `is_dir`, deactivating the exact path and
every child row in one statement. See
[006-directory-subtree-locations.md](../specs/006-directory-subtree-locations.md). This fix
landed without a cross-reference back to this entry — hence the stale OPEN status until now.

## 22. ~~Moving/renaming a directory is a silent no-op~~ — FIXED

**File:** `src/vcs/services/versioning.py:75` (`moved_handle`)

```python
context_id = _get_context_id_by_location(db_handler, event.dst)
update_path = Query(
    query="UPDATE locations SET location = ?, st_ino = ?, st_dev = ? WHERE context_id = ?",
    params=(event.dst, st_ino, st_dev, context_id),
)
```

`_get_context_id_by_location` stats `event.dst` and looks the row up by
`(st_ino, st_dev)`. For a directory that is the *directory's* inode, which has no row, so
`context_id` is `None` — and `WHERE context_id = NULL` matches nothing in SQL. The update
runs and changes zero rows, with no error.

Child rows keep pointing at paths under the **old** directory name. Every subsequent event
for those files then fails to match on path, so the tree effectively falls out of tracking.

**Currently masked, but not fixed.** On recursive watches both platforms also emit
synthetic per-child `FileMovedEvent`s via `generate_sub_moved_events`, and those *do* drive
`moved_handle` correctly per file — the child's inode is unchanged by a rename, so the
lookup succeeds. So directory moves appear to work today by accident. That masking is
fragile: it requires `recursive=True`, costs O(n) syscalls, and races an `os.walk` of the
destination that watchdog performs to synthesise the events.

Two further latent problems in the same function:

- `get_path_stats(event.dst)` is called **before** any DB work, so if `dst` has already
  been moved or deleted again it raises `FileNotFoundError` and the whole handler is lost.
  (Since the worker-durability fix it is logged and skipped rather than killing the thread.)
- Nothing reconciles `status` on a move. A directory renamed *out of* every configured
  source keeps `status = 1` until the next restart.

**Fix direction:** rewrite the path prefix for the whole subtree in one statement, before
touching the filesystem, and set `status` from `is_path_in_scope(event.dst)`. `st_ino` /
`st_dev` must **not** be rewritten — a rename does not change them, and writing the
directory's inode onto child rows would corrupt identity. (This SQL-prefix design was
superseded before being built — see
[STATE.md](STATE.md)
for what actually fixed this.)

**Re-audited 2026-09-09, confirmed fixed.** `dec14a3` implemented exactly this: `moved_handle`
(`versioning.py:98-129`) rewrites the whole subtree via one `substr`-prefixed `UPDATE`
*before* any filesystem stat, sets `status` from `is_path_in_scope(event.dst)`, and only
rewrites `st_ino`/`st_dev` on the exact-node branch (guarded by a `try/except
FileNotFoundError`, never touching the subtree rows). See
[006-directory-subtree-locations.md](../specs/006-directory-subtree-locations.md). Same stale
cross-reference gap as #21.

## 23. ~~MCP-triggered edits always commit to git as `unknown:filesystem`~~ — FIXED

**Files:** `src/app/mcp/server.py` (`write_file`/`create_file`/`delete_file`/`move_file`),
`src/utils/formatter.py:normalize_event`, `src/vcs/services/versioning.py:_resolve_actor`

The MCP write tools deliberately do plain filesystem I/O only — no direct call into
`vcs.services.versioning` — because the watcher already tracks every filesystem change
independently of who made it, and calling both would double-process the same edit (see the
module docstring in `server.py`). The watcher is therefore the *only* path that ever commits
to git, for MCP-driven writes exactly as for a human editor save.

That leaves no way to attribute the resulting commit to the calling agent. `normalize_event`
builds a `SourceEvent` straight from watchdog's `FileSystemEvent`, which carries no notion of
who triggered it — `event.actor` is unset. `_resolve_actor` (spec 007,
[007-actor-attribution.md](../specs/007-actor-attribution.md)) then falls back to the generic
`"unknown:filesystem"` label. So every MCP write, from any agent session, lands in git
attributed identically — indistinguishable from an unrelated human edit of the same file made
moments apart.

Confirmed still true after spec 008 ([008-mcp-read-version.md](../specs/008-mcp-read-version.md)),
which wired `read_file`'s `version` field to real git history but explicitly left this out as a
separate, harder problem.

**Fix direction:** correlating an MCP call with the watcher event it causes needs a short-lived,
path-keyed "pending actor" hint — set by the MCP tool immediately before its filesystem I/O,
read and cleared by the watcher's event handler when the matching event fires, with a TTL so a
later unrelated edit of the same path never inherits a stale binding. Debounce
(`WatchWorker._should_process`, 0.5s default) and OS event-delivery latency both need to fit
inside that TTL for the hint to still be there when the event lands.

**Fix:** implemented per that direction —
[013-actor-hints.md](../specs/013-actor-hints.md). The hint store is a small SQLite table
(`pending_actor_hints`), not an in-memory dict: the MCP server and the daemon are separate OS
processes (same constraint spec 012 hit for the repo lock), and SQLite is the store both
already share. `LocalConsumer.handle` consumes the hint right before dispatching to
`versioning.py`. CLI actor capture stays out of scope — no CLI command currently writes content
through the watcher path (`ctx rollback` attributes its own commit directly via `git_store`).

## 24. ~~Two test hermeticity gaps masked by real local dev files, broke CI~~ — FIXED

**Files:** `tests/unit/app/api/test_auth.py`, `src/app/mcp/server.py` (`_set_actor_hint`)

Found via `gh run view` on CI run `34274047251` (push of `0b45899`, spec 019) — 7 tests
failed uniformly across the whole Python matrix (3.10-3.14), `ruff` clean. Both root causes
are the same class of bug this project has hit before (issues #11/#21's "unit tests mock
the watcher" and the CI-fix commit `f8b073e`'s path bugs): a real, gitignored local file
made a test pass locally that fails on a clean checkout.

1. **`test_auth.py::test_ac3_correct_api_key_reaches_the_route`** — `FileNotFoundError:
   .../config.yaml`. The route it exercises (`GET /v1/sources`) calls through to
   `derive_watch_targets()` → `parse_config()`, which opens `CONFIG_PATH` directly. The
   test never isolated `CONFIG_PATH` (no `config_path` fixture, unlike
   `test_vcs_router.py`'s equivalent test), so it silently read the real repo-root
   `config.yaml` — present on every dev machine that's ever run the app, absent on a CI
   runner's clean checkout.
2. **Five `test_guardrail.py` tests** (`write_file`/`create_file`/`delete_file`/
   `move_file`) — `TypeError: expected str, bytes or os.PathLike object, not NoneType`,
   raised from `sqlite3.connect(None, ...)` inside `DBHandler.from_url`. `_set_actor_hint`'s
   docstring states the contract plainly: "an MCP call must never fail because hint
   bookkeeping couldn't complete" — but its `except sqlite3.Error` didn't cover this.
   `get_db_url()` returns `None` when `DATABASE_URL` is unset anywhere (no default, per
   `README.md`'s env table), which is exactly a CI runner's state with no `.env.dev`. Every
   dev machine's `.env.dev` (real, gitignored, always sets `DATABASE_URL`) masked this on
   every local run, including every gate this session ran before pushing.

**Fix:** `test_auth.py`'s `test_ac3` now uses the `config_path` fixture and writes a
minimal `sources: []` config, matching the isolation pattern already used everywhere else
in this test suite. `_set_actor_hint` now catches `(sqlite3.Error, TypeError)` — the
bookkeeping is still best-effort, matching its own stated contract; it just wasn't broad
enough to actually deliver on it. Verified by temporarily moving `config.yaml`/`.env`/
`.env.dev` aside and re-running the full suite locally (290 passed) before restoring them —
the same technique used to verify the earlier CI path-bug fix, since this machine has no
CI-identical environment to test against directly.

---

## 25. ~~`init_repo()` has no cross-process lock — racy `git init`/`git config` on a live daemon~~ — FIXED

**File:** `src/vcs/services/git_store.py` (`init_repo`)

Found live (2026-09-09) running the first real end-to-end pass: a real `ctx daemon`
against `knowledge.example/`, an MCP write via an in-process `fastmcp.Client` (standing in
for a real Claude Code session — see
[live-integration-test-plan.md](draft/live-integration-test-plan.md)), reproduced on the
first and second run alike, not a one-off flake:

```
subprocess.CalledProcessError: Command '['git', 'init']' returned non-zero exit status 128
```

raised from `current_version()` (called by the MCP `read_file` tool) immediately after
`create_file` wrote a new file straight to disk. Sequence: `create_file` writes the file,
the daemon's watcher picks up the filesystem event and starts committing it through
`versioning.py`'s handler (which calls `init_repo()` on the same mirror repo) at
essentially the same moment the MCP client's follow-up `read_file` also calls
`current_version()` → `init_repo()` on that repo. `write()` (`git_store.py:99`) is guarded
by the per-repo `_lock_for()` `FileLock` (spec 012); `init_repo()` is not — every one of its
6 call sites (`versioning.py:57,72,134,158,188,201`, plus 4 in `audit.py`) calls it
unlocked, including the two processes that just raced here. Re-running `git init` manually
in isolation right after (`git init` on an already-`.git` directory is normally a safe,
idempotent no-op — `Reinitialized existing Git repository...`, exit 0) succeeded every
time; it only fails when a second process's `git init`/`git config` subprocess calls
interleave with it against the same `.git` directory, which is exactly what two real OS
processes both reacting to the same fresh file do.

**Fixed:** spec [022](../specs/022-init-repo-cross-process-lock.md) — `init_repo()`'s body
now runs inside `with _lock_for(repo_path):`, the same cross-process `FileLock` `write()`/
`remove()`/`move()` already use. Regression test
(`test_ac1_init_repo_blocks_while_repo_lock_held`) proves the serialization directly (holds
the lock in the test thread, asserts a concurrent `init_repo()` call blocks until released)
rather than relying on timing to reproduce the original race. Re-verified live afterward via
[scripts/live_integration_test.py](../../scripts/live_integration_test.py) — clean pass.

## 26. `ctx daemon` process disappeared mid-run, zero log trace — root cause undetermined

**Files:** `data/ctx.log`, `src/app/cli/daemon.py`, `src/vcs/runtime.py`

Found in the same live-test pass as #25, same session. `ctx daemon start` was running
against `knowledge.example/`, actively processing real filesystem events (last log line at
09:42:35 was a normal `[SUCCEEDED] modified_handle`, no error, no exception, no traceback).
Some time later — after two plain `rm` calls on watched files that should have produced
`deleted_handle` log lines — `ctx daemon status` reported `stopped`, and `tasklist` confirmed
the PID from `data/ctx.pid` was genuinely gone, no `python.exe` process running at all.

**No trace of why.** Spec 017's SIGTERM handler logs `"Received signal %s, stopping..."` on
a clean stop; nothing in `data/ctx.log` says that, and nothing says an exception killed it
either — the log simply stops. That rules out a caught, logged failure and points at
something more abrupt: the process's thread crashing somewhere logging doesn't reach, or
the process being torn down from outside (a signal `runtime.py`'s handler doesn't cover, or
the parent shell/job-object relationship on Windows not fully detaching the child the way
`_spawn`'s `DETACHED_PROCESS`/`CREATE_NEW_PROCESS_GROUP` flags intend — this session's shell
tool is Git Bash over Windows, an environment `deploy/windows/register-ctx-daemon-task.ps1`
doesn't exercise, since a Scheduled Task launches independently of any interactive shell).

**Consequence, also live-confirmed:** restarting the daemon after this did **not** clean up
mirror state for the two files that had been deleted from the source while it was down —
`sync_source_status`'s restart-time reconciliation (see [STATE.md](STATE.md)) did not remove
them from the git mirror. This is exactly the gap [FUTURE.md](FUTURE.md) item 5 already
named as "not yet a live-observed problem" — it now is. Re-deleting the same files while the
daemon was alive and watching in real time propagated correctly (`deleted_handle`, mirror
updated) — so the gap is specifically the downtime window, not the delete path itself.

**Not investigated further here** — reproducing this reliably (was it the Git Bash shell
tearing down its process tree, a genuine crash, or something else) needs a dedicated repro
attempt outside this live-test pass, ideally from a real terminal instead of the agent's
shell tool. Flagging rather than guessing at a fix.

**Update, same day, via [scripts/live_integration_test.py](../../scripts/live_integration_test.py)'s
first automated run:** issue #27 below is very likely the real explanation. `_spawn()`
launches the daemon with `DETACHED_PROCESS` (no console at all), which is exactly the flag
combination Windows docs describe as unable to receive `GenerateConsoleCtrlEvent` later
(issue #27) - and, separately, a process with no console of its own is also the more fragile
half of "does it survive its parent shell tearing down" on Windows. Both symptoms trace back
to the same `_spawn()` creation-flag choice; fixing #27 properly (switching away from
`DETACHED_PROCESS`) should be evaluated for whether it also closes this one before treating
them as two independent bugs.

## 27. ~~`ctx daemon stop` crashes on every call on Windows — `CTRL_BREAK_EVENT` to a `DETACHED_PROCESS` child always fails~~ — FIXED

**File:** `src/app/cli/daemon.py` (`_spawn`, `_send_stop_signal`)

Found live (2026-09-09) on the first run of
[scripts/live_integration_test.py](../../scripts/live_integration_test.py) - the script's own
teardown step called `ctx daemon stop` on a daemon it had started, and the CLI crashed
instead of stopping it:

```
OSError: [WinError 87] The parameter is incorrect
```

raised from `os.kill(pid, signal.CTRL_BREAK_EVENT)` in `_send_stop_signal` (`daemon.py:67`).
Root cause: `_spawn()` (`daemon.py:52`) creates the daemon with
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`. `DETACHED_PROCESS` means the child has **no
console at all** - and `GenerateConsoleCtrlEvent` (what `os.kill(pid, CTRL_BREAK_EVENT)`
calls on Windows) can only target a process group that has a console, which
`CREATE_NEW_PROCESS_GROUP` alone would give it but `DETACHED_PROCESS` explicitly removes.
The two flags were combined for opposite reasons - `DETACHED_PROCESS` so the daemon isn't
tied to the launching terminal, `CREATE_NEW_PROCESS_GROUP` so it *can* receive
`CTRL_BREAK_EVENT` later - and Windows' actual behavior is that the second intent is defeated
by the first. This is not a one-off flake: every `ctx daemon stop` on Windows hits this,
100% reproducible, confirmed by a second manual run right after the scripted one.

**Consequence:** `ctx daemon stop` - spec 019's flagship feature, and the one
`deploy/windows/register-ctx-daemon-task.ps1` and
[docs/runbook-shared-install.md](../../runbook-shared-install.md) both assume works - is
currently non-functional on Windows. `stop()`'s own force-kill fallback (`_force_kill`,
`taskkill /F`) never runs, because the crash happens before that code path is reached, not
after a timeout. `ctx daemon status` and `ctx daemon start` are unaffected - only `stop`
calls `_send_stop_signal`. Worked around in this run via a direct `taskkill /F /PID <pid>`
outside the CLI.

**Fixed:** spec [023](../specs/023-daemon-stop-windows-cross-process.md). The actual fix
differs from the "just drop `DETACHED_PROCESS`" guess above in one detail, found by
experiment: `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` **also fails** the same way -
`CREATE_NO_WINDOW` means no console is allocated at all, not just no *visible* window, same
underlying problem as `DETACHED_PROCESS`. The combination that actually works cross-process
(four flag/target combinations tested against a real child process) is
`CREATE_NEW_PROCESS_GROUP` alone plus a `STARTUPINFO`/`SW_HIDE` to hide the now-real
console's window after the fact. Separately, `vcs/runtime.py` only registered `SIGTERM`,
never `SIGBREAK` (what `CTRL_BREAK_EVENT` maps to in Python on Windows) - confirmed by
experiment that an unhandled `SIGBREAK` terminates the process outright, bypassing
`run()`'s `except KeyboardInterrupt`/`self.stop()` cleanup entirely; both halves were
needed. The regression test had to be rewritten once already during this fix: a first
version spawned the "stop" step as a direct subprocess of the test process itself, which
passed even against the *unfixed* code - Windows apparently grants a creator process's own
descendants the same signal rights as the creator, so it never reproduced the real bug.
The version that actually catches it runs `start()` and `stop()` as two fully independent,
sibling OS processes (the starter already exited before the stopper runs), matching real
`ctx daemon start` / `ctx daemon stop` CLI usage exactly -
`test_ac1_stop_from_a_separate_process_does_not_raise`
(`tests/integration/app/cli/test_daemon_lifecycle.py`). Re-verified live via both the direct
CLI (`uv run ctx daemon start` then a separate `uv run ctx daemon stop` - clean, fast) and
[scripts/live_integration_test.py](../../scripts/live_integration_test.py) (teardown now
passes through the real `stop()` path, not the fallback force-kill it also carries).
**#26 not independently re-confirmed** - plausible this was its root cause all along, but
left open pending an actual fresh live observation, not closed on inference alone.

## 28. ~~`rollback_session` crashes when an actor's earliest touch on a path predates that path's own history in a shared mirror repo~~ — FIXED

**File:** `src/vcs/services/audit.py` (`rollback_session`)

Found live (2026-09-09), same automated run as #27, in `scripts/live_integration_test.py`'s
`E.rollback_session` step:

```
CalledProcessError: Command '['git', 'show', '180a5a75...:live-test-probe.md']'
returned non-zero exit status 128
```

`rollback_session` (`audit.py:141-156`) treats `earliest["parent"] is None` as the *only*
signal that an actor created a path (so it should be deleted, not reverted to parent
content). That's correct only when the actor's earliest commit is the literal first commit
in the whole mirror repo's history. Mirror repos are shared per watch-target *directory*
(one repo can hold many files - `knowledge.example/docs/` mirrors `example_1.pdf`,
`example_2.pdf`, `example_3.pdf`, and, in this run, the freshly created
`live-test-probe.md`). The very first commit that ever touches a given *path* almost always
has a non-`None` parent - the repo already has commits from other files - but that parent
commit's tree still doesn't contain the new path, so `git show parent:relpath` fails exactly
as seen here. `earliest["parent"] is None` under-detects "this actor created the path"; the
correct check is whether `relpath` existed in `earliest["parent"]`'s tree, not whether
`earliest["parent"]` exists at all.

**Why the manual live-test pass (this session, same day) didn't hit it:** that pass reused
`knowledge.example/prompts/live-test-probe.md` across several runs, so by the time
`rollback-session` ran, the path already had its own multi-commit history within that actor's
earliest commit's parent - the bug needs a path's *genuine* first-ever commit in a
*non-empty* repo to surface, which the automated script's `docs/` target (also holding 3
baseline PDFs) hit on its very first run.

**Consequence:** `ctx rollback-session` crashes (uncaught `CalledProcessError`, CLI exits 1)
instead of deleting the newly-created path, for any actor whose *only* contribution in a
given repo is creating a brand-new file - arguably the single most common case for an
MCP-driven session (`create_file` then a few edits). Worked around in this run via a direct
file `unlink()` in the script's cleanup step, bypassing `rollback_session` entirely for that
path.

**Fixed:** spec [024](../specs/024-rollback-session-shared-repo-create.md). New primitive
`git_store.path_exists_at_rev(repo_path, relpath, rev)` (`git cat-file -e`, never raises for
a missing path - unlike `show()`); `rollback_session`'s create-vs-modify check became
`earliest["parent"] is None or not path_exists_at_rev(...)`, covering both the original
empty-repo case and this one. Regression test reproduces the exact shape that crashed live:
an unrelated file committed first (different author), then the target actor creates a
brand-new path in the same repo - `rollback_session` now deletes it instead of crashing.
Re-verified live via [scripts/live_integration_test.py](../../scripts/live_integration_test.py) -
`E.rollback_session` now passes.

## 29. ~~`_lock_for()`'s cross-process `FileLock` has no timeout — a contended write hangs forever, not until any bounded 30s~~ — FIXED

**File:** `src/vcs/services/git_store.py:26-31`

```python
def _lock_for(repo_path: Path) -> FileLock:
    key = str(repo_path.resolve())
    with _locks_guard:
        if key not in _locks:
            _locks[key] = FileLock(str(repo_path / ".chrono-ctx.lock"))
        return _locks[key]
```

No `timeout=` argument. `filelock.FileLock`'s default is `-1` (confirmed via
`inspect.signature(FileLock.__init__)`) - block indefinitely. Every
`write()`/`remove()`/`move()`/`init_repo()`/`reset_stale_index()` call
acquires this lock via `with _lock_for(repo_path):`, so any of them can
stall forever if another process is holding it.

Found live (2026-09-09) testing the MCP server with 3 concurrent Claude
Code sessions all pointed at this same project (`claude mcp list`/process
listing confirmed 3 independent `uv run python -m app.mcp.server`
instances, each spawned by a different session, all sharing the same
`config.yaml` and therefore the same mirror repos). An MCP `write_file`
call (`app/mcp/server.py`) that goes through `_check_expected_version()` →
`current_version()` → `git_store.init_repo()`/`head_rev()` hit exactly this
lock. This was never a real risk with a single daemon as the only writer
(spec 022's AC-1 explicitly chose "blocks until released" as correct
behavior, written when only one contending caller was ever realistic) -
it becomes one the moment multiple independent processes routinely touch
the same mirror repo, which concurrent MCP sessions now make normal.

**Symptom, live-observed:** a `write_file` call hung, then eventually
surfaced `Connection closed` (once) and, on a retry, `Error calling tool
'write_file': Command '['git', 'config', 'user.email', ...]' returned
non-zero exit status 1` (once) - consistent with the MCP client's own
transport-level timeout (~30s) giving up on a request that was blocked
server-side with no bound of its own, rather than chrono-ctx itself ever
deciding to fail fast. The exact `git config` exit-1 shape wasn't
reproduced a second time and may be a separate, transient issue (e.g.
Windows AV/OneDrive interference on a freshly-created `.git` dir) - not
confirmed as the same root cause, flagged rather than assumed.

Ruled out as the primary cause: `DBHandler.from_url()`'s SQLite
`timeout=30.0` (issue #20/spec 016) - `_set_actor_hint()` is the only MCP
call-path user of it, and it silently swallows `sqlite3.Error`
(best-effort hint bookkeeping), so it can only produce a silent ~30s
stall, never a visible trace - doesn't match what was actually observed.

**Consequence:** any MCP write/delete/move call (or CLI/API call) against
a mirror repo another process is mid-operation on has no bounded wait and
no clean error - it either eventually succeeds after an arbitrarily long
stall, or the *caller's* own unrelated timeout mechanism aborts it first,
surfacing a confusing transport-level error instead of a clear
"repo busy, try again" from chrono-ctx itself.

**Fixed:** spec [036](../specs/036-git-store-lock-timeout.md). `_lock_for()`
now passes `timeout=LOCK_TIMEOUT` (30.0, matching `DBHandler`'s existing
convention) to `FileLock(...)` - a contended lock now raises
`filelock.Timeout` (an `OSError` subclass) instead of blocking forever.
`app/mcp/server.py`'s two previously-unwrapped `current_version()` call
sites (`read_file`, `_check_expected_version()` used by
`write_file`/`delete_file`) are now inside the existing `except IO_ERRORS`
boundary, so a timeout returns `{"status": "error", "reason": ...}`
instead of leaking an unhandled exception through the MCP transport.
Live-verified with the real (unmocked) code path: a script held the lock
in one thread while `write()` ran in another with `LOCK_TIMEOUT` set to
0.5s - raised `filelock.Timeout` at ~0.52s elapsed, not indefinitely;
normal daemon operation (uncontended write via the watcher) unaffected.
