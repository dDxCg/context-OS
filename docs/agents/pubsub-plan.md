# Local pub/sub architecture for VCS

Status: **implemented.** See [rabbitmq-migration.md](rabbitmq-migration.md) for the
broker-side end state this is shaped for.

## Context

Routing used to live in application code. `LocalRuntime._route` did an `isinstance` demux
and picked a queue object by hand, and `stop()` published `STOP` to each queue
individually. Producers therefore had to know how many consumers existed and which queue
each one owned.

That coupling is what caused [issues.md](issues.md) #1 (one `STOP`, two blocked readers),
and it re-appeared later as a queue-aliasing regression where `_route` published to a queue
the consumer worker was not reading from — silently dropping every source event and hanging
shutdown. Every new consumer meant editing the router *and* `stop()`.

In pub/sub a producer publishes **once** with a routing key and the broker fans out to
bound queues. Moving routing behind a bus removes that bug class structurally.

## Design

```
WatchWorker ──► LocalRuntime._publish ──► LocalEventBus.publish
                (cache coherence only)      │  topic_for(event)
                                            ├─► Subscription("source.#", where=_in_scope)
                                            │      └─► LocalQueue ──► ConsumerWorker
                                            └─► Subscription("config.#")
                                                   └─► LocalQueue ──► ConfigConsumerWorker
```

| Concept | Here | RabbitMQ |
|---|---|---|
| `EventBus` | `LocalEventBus` | exchange (`vcs.events`, topic) |
| `Subscription` | queue + binding + predicate | queue + binding |
| `pattern` | `"source.#"` | binding key |
| `EventBroker` / `LocalQueue` | the mailbox | the queue |
| `bus.close()` | `STOP` to every mailbox | `basic_cancel` / connection close |

### Two topics, forward-compatible bindings

Routing keys are coarse — the **category**, not the verb: `source` and `config`
([types.py](../src/vcs/shared/types.py) `topic_for`). The verb stays an `isinstance`
concern inside each consumer, exactly as before.

Subscribers bind `source.#` / `config.#` rather than exact-matching. In AMQP topic
exchanges `#` matches **zero or more** words, so those patterns match the bare keys
published today *and* a finer `source.created` published later. **Routing can be refined
without touching a single binding** — the coarse scheme is not a one-way door.

The legacy `type` field (`"added"`/`"modified"`/…) is untouched; it is unused by dispatch
and renaming it would break for no gain.

### Scope filtering: consumer-declared, fanout-evaluated

The source subscription carries the predicate; the config subscription does not, so config
events are exempt **by construction** rather than by an early return in a router:

```python
self.source_sub = self.bus.subscribe("source.#", where=self._in_scope)
self.config_sub = self.bus.subscribe("config.#")
```

**A deliberate divergence from RabbitMQ, recorded rather than glossed:** the predicate is
evaluated by the bus during fanout, on the *publishing* thread — not inside the consumer
thread. `_in_scope` reads `_scope_cache`, whose lock-free design depends on only watchdog's
single dispatcher thread touching it. Evaluating it on the consumer thread would make that
cache genuinely cross-thread and require a lock. So: **policy is consumer-declared,
evaluation is at fanout.** In RabbitMQ this becomes either a consumer-side check or a finer
binding.

### Cache coherence, not routing

`LocalRuntime._publish` is a three-line wrapper that invalidates `_scope_cache` on a config
event before delegating to `bus.publish`. This has to happen on the publishing thread so
the *very next* source event is matched against the new scope, rather than waiting for the
config worker to drain its queue. It is deliberately not a bus hook — RabbitMQ has no
equivalent concept, and inventing one would not survive the migration.

### Serialization

`event_to_dict` / `event_from_dict` with an `event` discriminator
(`"source.modified"`, `"config.moved"`, …). Unused locally — events pass by reference — but
built now because it is the piece most likely to force a redesign later, and it is cheap
and unit-testable today. The wire names are **finer than the routing keys on purpose**:
they already carry the verb, so refining routing needs no new vocabulary.

Two sharp edges handled: `MovedEvent` is `kw_only` and carries `dst`; the four
`Config*Event` classes default `src` from a factory reading the *current* config path, so
`from_dict` passes `src` explicitly — otherwise a round-trip silently rewrites `src` to
whatever `CONFIG_PATH` is on the consuming side. Both are covered by tests.

## Constraints that must not be broken

1. **Delivery stays asynchronous, via per-subscriber queues.** A synchronous bus would run
   handlers on watchdog's dispatcher thread while it holds `BaseObserver._lock`, putting DB
   writes on the watcher thread (cross-thread sqlite) and deadlocking against
   `ConfigConsumer`'s `watcher.reconcile()`, which needs that same lock.
2. **Mailboxes stay unbounded.** `publish` runs on that same dispatcher thread; a bounded
   queue could block there and deadlock against `reconcile()` identically.
3. **A failing predicate must not escape `publish`.** The publisher *is* the dispatcher
   thread; losing it would silently stop all watching. `LocalEventBus.publish` logs and
   skips.

## Files

| File | Change |
|---|---|
| [src/vcs/workers/bus.py](../src/vcs/workers/bus.py) | **new** — `EventBus`, `Subscription`, `LocalEventBus`, `topic_matches` |
| [src/vcs/shared/types.py](../src/vcs/shared/types.py) | topics, `topic_for`, wire-name registry, `event_to_dict`/`event_from_dict` |
| [src/vcs/workers/local/local_runtime.py](../src/vcs/workers/local/local_runtime.py) | `_route`/`_route_config_only` deleted; subscriptions; `stop()` → `bus.close()` |

`ConsumerWorker`, `ConfigConsumerWorker`, `LocalQueue`, and `EventBroker` are **unchanged** —
the bus sits in front of the existing mailbox abstraction. That containment is the main
evidence the refactor is safe.

## Verification

`uv run pytest` — **158 passed** (was 110); `uv run ruff check .` clean.

End-to-end, because unit tests mock the watcher and cannot see any of this:

| Check | Result |
|---|---|
| `live_test.py` harness (full MCP ↔ runtime path) | **16/16**, unchanged |
| Unapproved sibling under a watched directory not versioned | pass |
| Unrelated sibling of `config.yaml` not versioned (`_route_config_only`'s old job) | pass |
| Config edit applies to the very next source event | pass |
| `stop()` returns promptly; both workers exit via one `bus.close()` | pass |

The third row is the one that mattered most: it is behaviour that *moved* from a dedicated
router into the scope predicate, rather than behaviour preserved by construction.

## Not done here

- **Ack/nack and redelivery.** No local consumer needs it; `ConsumerWorker` already logs and
  continues on handler failure. Add it with the real broker, where it has meaning — and note
  it likely requires growing the `EventBroker` interface.
- **Any `pika`/`aio-pika` code.** This change only shapes the seam.
