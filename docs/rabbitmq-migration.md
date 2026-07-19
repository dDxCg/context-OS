# Event broker: path to RabbitMQ

Design notes for moving `vcs.workers.interfaces.event_broker.EventBroker`
(today only implemented by the in-memory `LocalQueue`) toward RabbitMQ, and
what to do locally *now* so that migration is a broker swap, not a rewrite.

## Why

Immediate motivation is [issues.md](issues.md) #1 and #10:

- **#1** — `ConsumerWorker` and `ConfigConsumerWorker` are wired to the same
  `LocalQueue` instance. Confirmed live: a plain `CreatedEvent` got dequeued
  by `ConfigConsumerWorker` instead of `ConsumerWorker` and silently dropped
  (`ConfigConsumer.handle()` no-ops on non-`Config*` events). Also causes a
  shutdown hang — only one worker ever dequeues the single `STOP` sentinel.
- **#10** — the config file itself is never registered as a watch target,
  so config hot-reload never fires regardless of #1.

Longer-term motivation for RabbitMQ specifically (not just fixing the
in-memory queue): `config.example.yaml` already stubs a `3rd-party` source
type, and [note/note.md](note/note.md) already sketches "api gateway ->
provider adapter (webhook native + polling rest) -> sqlite" for those. Once
a source adapter can run out-of-process (a webhook receiver, a polling
service), an in-memory `queue.Queue` can't carry events across that process
boundary — that's the actual job RabbitMQ does here that `LocalQueue` can't.

## Target topology (once RabbitMQ lands)

- One topic exchange, e.g. `vcs.events`.
- Routing key per event category: `source.created`, `source.modified`,
  `source.deleted`, `source.moved`, `config.created`, `config.modified`,
  `config.deleted`, `config.moved`.
- Two durable queues bound with wildcard patterns:
  - `source.#` → the main consumer queue (`LocalConsumer`'s role today).
  - `config.#` → the config consumer queue (`ConfigConsumer`'s role today).
- Future producers (3rd-party adapters) publish onto the same exchange with
  their own routing keys (e.g. `notion.source.modified`) — consumers don't
  need to change, only their binding patterns would, if ever.

## Interim step — do this locally now, independent of RabbitMQ landing

This is the part that should happen regardless of when/whether RabbitMQ
actually gets adopted, because it's also the correct fix for #1 and #10.
Superseded from an earlier draft of this doc (per `docs/issues.md` issue
#1, "Option C"): **one queue, one consumer**, not one queue per consumer —
simpler, and structurally removes the race rather than routing around it.

1. **Collapse the two worker threads into one.** `ConsumerWorker`+
   `LocalConsumer` and `ConfigConsumerWorker`+`ConfigConsumer` merge into a
   single worker reading a single `LocalQueue`, with one dispatch entry
   point branching on event type — the same `isinstance` pattern
   `LocalConsumer.handle()` already uses for `Moved`/`Modified`/`Deleted`/
   `Created`, now also covering `Config*Event`. With only one reader, the
   race that causes issue #1 (silent event loss to the wrong consumer,
   shutdown hang on the single `STOP`) is structurally impossible — there's
   no second thread left to lose it to.
2. **Handling a `Config*Event` does, in order:**
   - Recover/re-parse config for deleted/moved events, as today.
   - **Refresh the permission/allowed-path cache** — this is a *derived
     view*, kept in sync for convenience, not the authorization boundary
     itself. See "MCP guardrail" below: real enforcement is synchronous and
     independent of this queue, so this refresh is allowed to lag like any
     other bookkeeping step.
   - **Re-run `sync_source_status`** (`src/vcs/services/versioning.py`) so
     `locations.status` reflects the current source list on every config
     change, not just at `Initializer.init()` startup. Also allowed to lag
     — same reasoning.
   - Diff sources (`get_config_diff`, unchanged), collect files for
     newly-added sources, publish their `CreatedEvent`s back onto the
     *same* queue.
   - Add/remove watches (issue #10, still a prerequisite either way).
   - Because everything above runs on one FIFO queue with one consumer
     thread, this bookkeeping always happens-before the `CreatedEvent`s it
     unlocks for a newly-added source — correct ordering for free, no
     cross-queue coordination to get right. This is a nice property to have,
     but per the guardrail below, it is no longer what correctness for MCP
     access control rests on.
3. **Register the config file itself as its own watch target** (fixes
   #10) — `watcher.add_watch(config_path, callback=...)` alongside the
   per-source watches in `LocalRuntime._init_worker`, publishing onto the
   same single queue as everything else.
4. **`LocalRuntime.stop()` goes back to one `STOP` sentinel and one
   `.join()`.** The dual-worker-thread split introduced in the `3630dc5`
   refactor is what created the race in the first place; this removes the
   second thread instead of patching around it. Trade-off: no parallelism
   between config-adapt work and versioning work anymore (one thread does
   both, sequentially) — acceptable given both are fast, file-local
   operations, and correctness beats throughput two racing threads never
   actually delivered anyway.

This doesn't foreclose the two-queue RabbitMQ topology above — that's a
broker-side routing choice independent of how many local consumer threads
read from it. Even a future RabbitMQ setup could have a single consumer
process bind both `source.#` and `config.#` onto one queue, mirroring this
local design; or split into two consumer processes once there's an actual
reason to (independent scaling, isolating a slow handler) rather than
because of a bug. The two sections of this doc aren't in tension.

## MCP guardrail — synchronous, separate from the event queue

Resolved design point: **enforcement is a separate, synchronous guardrail
in front of the MCP tools, not something derived from queue-processed
state.**

- `read_file`/`write_file`/`create_file`/`delete_file`/`move_file`
  (`src/app/mcp/server.py`, currently stubs) check the requested path
  against the **current** allowed-source list *at call time*, querying live
  config/DB state directly — not "has the relevant `Config*Event` worked
  its way through the queue yet."
- **If the path is out of scope (not part of the current source list),
  block the call.** This is a hard rule, enforced synchronously, on every
  call — independent of whatever the event-queue consumer has or hasn't
  processed yet.
- Because enforcement never depends on queue state, the queue-driven side
  (permission-cache refresh, `sync_source_status`, `locations.status`) is
  free to be eventually consistent — **status can lag**; it's bookkeeping
  that makes the DB/cache reflect reality promptly as a convenience, not
  the thing standing between a request and unauthorized access.
- This is what resolves the single-vs-parallel-queue trade-off from
  earlier: once MCP correctness no longer depends on queue ordering, the
  choice between "Interim step" above (one queue) and the per-consumer-queue
  alternative in `docs/issues.md` issue #1 (Option A) stops being a
  correctness question. It becomes a pure throughput/latency trade-off,
  revisited only if there's an actual reason to (see issue #1's updated
  recommendation).
- Storage/lookup shape for the guardrail's live check (in-memory set kept
  fresh by the queue-driven refresh above vs. a direct DB query per call vs.
  something else) is **not decided** — only that the check itself is
  synchronous and sits in front of the MCP tools, independent of the queue.

## The seam for the future broker

`EventBroker.publish(event)` / `.consume()` / `.close()` stays the
abstraction boundary. A future `RabbitMQBroker(EventBroker)` implements the
same three methods against `pika`/`aio-pika`. What's different there and
does *not* apply to the local interim step:

- **Serialization.** In-process, an event object is passed by reference.
  Over RabbitMQ it has to become bytes — worth adding `to_dict()`/
  `from_dict()` on `SourceEvent` (with a `type` discriminator to reconstruct
  the right subclass) when this is actually built, even though `LocalQueue`
  never needs it.
- **Connection/channel lifecycle.** `RabbitMQBroker` owns a connection and
  channel per consumer (or a shared connection with per-consumer channels),
  needs reconnect/retry handling that `LocalQueue` has no analog for.
- **Ack/nack semantics.** Today `LocalQueue.close()` calls
  `queue.task_done()` — that's already the wrong analogy (flagged in
  issues.md #1) and should not be carried into `RabbitMQBroker.close()`,
  which should just close the channel/connection. A real broker also needs
  `consume()` to ack (or nack/requeue on handler failure) rather than the
  current fire-and-forget dequeue.

## Open questions

- Route by exact event type (`source.created`) or a coarser category
  (`source.*`) for the eventual RabbitMQ binding patterns — not a decision
  the local step needs, since it dispatches by `isinstance` on one queue,
  not by routing key.
- Whether to build `RabbitMQBroker` behind the same `EventBroker` interface
  as-is, or whether the interface itself needs to grow (e.g. `ack()`) once
  a real broker with delivery guarantees is in the picture.
- Exchange/queue naming and durability/TTL conventions — deferred until
  RabbitMQ is actually being stood up, not decided here.
- ~~What storage/lookup shape the guardrail's live check uses~~ —
  **implemented**: `vcs.services.configure.is_path_in_scope()` re-parses
  `config.yaml` live on every call (no cache) and checks whether the target
  path equals or is nested under a configured `local` source. Simplest
  correct thing given call volume is low; revisit only if `parse_config()`
  (a file read + YAML parse per call) ever shows up as a real cost.
  `src/app/mcp/guardrail.py::ensure_scope()` wires it to `Context.elicit()`
  for the approve/decline flow, fails closed (blocks) on decline, cancel,
  or a client that errors/doesn't support elicitation. Gates all five MCP
  tools (`read_file`/`write_file`/`create_file`/`delete_file`/`move_file`
  — `move_file` checks both `src` and `dst`). Approving adds the exact
  requested file path to `config.yaml`'s sources (not its parent
  directory) — narrowest grant, sibling files still prompt separately.
  Covered by `tests/unit/app/mcp/test_guardrail.py` (uses
  `fastmcp.Client(server.mcp, elicitation_handler=...)` for a fully
  in-process approve/decline simulation) and
  `tests/unit/vcs/services/test_configure.py`.
- Whether to implement the interim local step (above) as its own follow-up
  task now, or bundle it with whichever of #1/#10 gets picked up first.
