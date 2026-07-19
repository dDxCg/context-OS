# Known bugs — config hot-reload refactor

Found during code review of the worker-abstraction refactor (`3630dc5`) plus the
uncommitted fix-in-progress on top of it. Ranked most severe first.

Open: **#1** (shared queue race) and **#10** (config file never watched,
found via live-testing). Fixed: #2-#9, each with a regression test that's
no longer `xfail` (except #9, found and fixed via live-testing rather than
a unit test — see its entry).

## 1. Shared queue race causes indefinite shutdown block

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
  in [rabbitmq-migration.md](rabbitmq-migration.md)): MCP tool calls check
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

Not yet implemented — left as an evaluation per request; #2-#9 below were
fixed, #1 (and #10, which Option C still needs as a prerequisite) intentionally
were not.

**Update:** the direction is now to eventually move `EventBroker` to
RabbitMQ (multiple, possibly out-of-process producers — see the `3rd-party`
source type already stubbed in `config.example.yaml`). This doesn't
conflict with Option C above: the RabbitMQ *topology* (two queues,
`source.#`/`config.#`, bound from one exchange) is a broker-side routing
choice independent of how many local consumer threads read them — even a
single future RabbitMQ consumer could bind both patterns onto one queue,
mirroring C. See [rabbitmq-migration.md](rabbitmq-migration.md) for the
full topology and interim-step design, now updated to describe Option C.

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

## 10. Config file itself is never watched — hot-reload is dead code today

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

**Fix direction:** register a watch on `Path(get_config_path()).parent` (or
the file itself, depending on what the `watchdog` backend supports reliably
across platforms) pointing at the config queue, alongside the per-source
watches in `_init_worker` — this pairs naturally with the issue #1 fix
(option A), which already needs a dedicated `callback=config_consumer_worker
.queue.publish` for exactly this watch.

---

*Cross-referenced in [note.md](note/note.md) under "Known issues".*
