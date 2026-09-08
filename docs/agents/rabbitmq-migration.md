# Event broker: path to RabbitMQ

Design notes for moving `vcs.workers.bus.EventBus` (today only implemented by
the in-process `LocalEventBus`) toward RabbitMQ.

The local pub/sub layer is **implemented** — see [pubsub-plan.md](pubsub-plan.md).
This doc covers what is left for a real broker, and records where the local
implementation deliberately diverges.

## Why

The original motivation was [issues.md](issues.md) #1 and #10, both since fixed:

- **#1** — `ConsumerWorker` and `ConfigConsumerWorker` shared one `LocalQueue`.
  Confirmed live: a plain `CreatedEvent` was dequeued by `ConfigConsumerWorker`
  and silently dropped, plus a shutdown hang because only one worker ever
  dequeued the single `STOP`. Fixed, then structurally eliminated by the bus —
  `bus.close()` fans `STOP` out to every subscription, so shutdown can no
  longer drift out of sync with the consumer set.
- **#10** — the config file was never a watch target, so hot-reload never
  fired. Fixed.

Longer-term motivation for RabbitMQ specifically (not just fixing the in-memory
queue): `config.example.yaml` already stubs a `3rd-party` source type, and
[note/note.md](note/note.md) sketches "api gateway -> provider adapter (webhook
native + polling rest) -> sqlite" for those. Once a source adapter runs
out-of-process (a webhook receiver, a polling service), an in-memory
`queue.Queue` cannot carry events across that process boundary — that is the
actual job RabbitMQ does here that `LocalQueue` cannot.

## Target topology

- One topic exchange, e.g. `vcs.events`.
- Two durable queues bound with wildcard patterns:
  - `source.#` → the source consumer queue (`LocalConsumer`'s role).
  - `config.#` → the config consumer queue (`ConfigConsumer`'s role).
- Future producers (3rd-party adapters) publish onto the same exchange with
  their own routing keys (e.g. `notion.source.modified`) — consumers do not
  need to change, only their binding patterns would, if ever.

### Routing keys are coarse today, and that is safe

The local bus publishes **two** routing keys — `source` and `config` — derived
from the event category, not the verb. An earlier draft of this doc assumed one
key per verb (`source.created`, `source.modified`, …).

Both work, because the **bindings** are `source.#` / `config.#` and in AMQP
topic exchanges `#` matches **zero or more** words. `source.#` therefore matches
the bare key `source` published today *and* a finer `source.created` published
later. Publishing can be refined at any point **without touching a binding**.

The finer names already exist as the serialization discriminator
(`event_name()` → `"source.modified"`), so refining routing needs no new
vocabulary — it is a one-line change in the publisher.

## What the local implementation already provides

| Concept | Local | RabbitMQ |
|---|---|---|
| `EventBus.publish/subscribe/close` | `LocalEventBus` | exchange |
| `Subscription(pattern, queue, where)` | binding + mailbox | queue + binding |
| `topic_matches()` | AMQP `*`/`#` semantics, unit-tested | server-side |
| `event_to_dict`/`event_from_dict` | dict with `event` discriminator | message body |
| `bus.close()` | `STOP` to every mailbox | `basic_cancel` / close |

`ConsumerWorker` consumes from `EventBroker`/`LocalQueue` and is unaware of the
bus, so a broker swap does not touch consumer code.

## What is still missing for a real broker

- **Bytes on the wire.** `event_to_dict`/`event_from_dict` exist and are tested
  (including the two sharp edges: `MovedEvent.dst`, and `Config*Event`'s
  `default_factory` for `src` which would otherwise rewrite `src` on the
  consuming side). Still needs a codec choice — JSON is the obvious default —
  plus content-type and schema-version headers.
- **Connection/channel lifecycle.** `RabbitMQBus` owns a connection and a
  channel per consumer, and needs reconnect/retry that `LocalEventBus` has no
  analog for.
- **Ack/nack semantics.** Today `ConsumerWorker` logs a handler failure and
  continues — the event is dropped. A real broker should ack on success and
  nack (requeue or dead-letter) on failure. **This likely requires growing the
  `EventBroker` interface** (an `ack()`/`nack()` on the consumed message rather
  than a bare event), which is the one interface change the migration is
  expected to force.
- **Predicate placement.** See the divergence below.

## Known divergence: where the scope predicate runs

`Subscription.where` is declared by the subscriber but **evaluated by the bus
during fanout, on the publishing thread** — not in the consumer thread.

This is deliberate. `LocalRuntime._in_scope` reads `_scope_cache`, whose
lock-free design depends on only watchdog's single dispatcher thread touching
it. Evaluating the predicate on a consumer thread would make that cache
genuinely cross-thread and require a lock.

RabbitMQ has no equivalent of a fanout-time predicate. On migration it becomes
either:

- a **consumer-side check** at the top of the handler — closest to the current
  semantics, but the scope cache then needs a lock or a re-read; or
- a **finer binding**, if scope can be expressed as a routing-key pattern —
  cleaner, but scope is a path-prefix test, which routing keys cannot express.

The consumer-side check is the likely answer. Do not assume the local
`where=` maps 1:1 onto a broker feature.

## Threading constraints that survive any broker change

1. Delivery must stay **asynchronous**. Running handlers inline would execute
   them on watchdog's dispatcher thread while it holds `BaseObserver._lock`,
   putting DB writes on the watcher thread and deadlocking against
   `ConfigConsumer`'s `watcher.reconcile()`, which needs that same lock.
2. Mailboxes must stay **unbounded**, for the same reason: a blocking `put()`
   on the dispatcher thread deadlocks against `reconcile()`.
3. A failing predicate must not escape `publish()` — the publisher *is* the
   dispatcher thread, and losing it silently stops all watching.

## MCP guardrail — synchronous, separate from the event bus

Unchanged and still correct: enforcement is a synchronous guardrail in front of
the MCP tools, **not** derived from queue-processed state.

`vcs.services.configure.is_path_in_scope()` re-parses `config.yaml` live on
every call and checks whether the target equals or is nested under a configured
`local` source. `src/app/mcp/guardrail.py::ensure_scope()` wires it to
`Context.elicit()` for approve/decline, fails closed on decline, cancel, or a
client that errors or lacks elicitation support. It gates all five MCP tools
(`move_file` checks both `src` and `dst`). Approval grants the exact requested
path, not its parent — narrowest grant; siblings still prompt separately.

Because enforcement never depends on bus state, everything the bus drives
(`locations.status`, watch reconciliation) is free to be eventually consistent.
Status may lag; the guardrail may not.

## Open questions

- Whether `EventBroker` grows `ack()`/`nack()`, or whether `consume()` starts
  returning a message wrapper. Deferred until a broker with delivery guarantees
  is actually in the picture.
- Exchange/queue naming, durability, and TTL conventions — deferred until
  RabbitMQ is being stood up.
- Whether the scope predicate becomes a consumer-side check or something else
  (see divergence above).
- ~~Route by exact event type or a coarser category~~ — **decided**: coarse
  (`source`/`config`), with `#` bindings keeping the finer option open.
- ~~`LocalQueue.close()` misusing `task_done()`~~ — **fixed**; it is now an
  idempotent flag. Do not carry the `task_done` analogy into a broker `close()`,
  which should just close the channel/connection.
