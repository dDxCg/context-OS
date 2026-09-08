# Spec — central-writer topology for multi-server deployment

Status: **proposed, not started.** Implements
[cowork-enterprise-plan.md](../cowork-enterprise-plan.md) Phase 4 — the
"scale / centralize view" direction from the distributed-versioning
comparison in that doc. Extends [rabbitmq-migration.md](../rabbitmq-migration.md),
does not replace it; this spec is what the exchange/binding topology
described there is *for*.

## Topology

```
Edge server A ─┐
Edge server B ─┼─► vcs.events (topic exchange) ─► central-writer queue(s) ─► git mirror repos
Edge server N ─┘        source.# / config.#
```

Edge servers run the watcher only — no local git mirror, no `LocalConsumer`
doing `versioning.py` work. They publish events (already the exact shape
`event_to_dict` produces, per [types.py](../../../src/vcs/shared/types.py),
extended with `actor` per [actor-attribution.md](actor-attribution.md)) onto
the exchange and stop. All writes happen at exactly one logical place: the
central writer, which owns the single write lock
[git-backend-plan.md](../git-backend-plan.md) already requires per repo. This
sidesteps distributed git entirely — no remotes, no push/pull, no merge —
by construction, since there is structurally only one writer.

## Partitioning — one consumer per repo, not a shared pool

[git-backend-plan.md](../git-backend-plan.md) uses per-directory mirror
repos. A single global consumer thread serializes writes across *every*
repo, which is correct but throws away the concurrency that owning separate
repos was supposed to buy. Instead:

- One durable queue per repo (bound with the routing key derived from the
  source path, e.g. `source.<repo-id>`), each with exactly one consumer
  thread/process.
- Ordering is only guaranteed **within** a queue (per repo) — this is
  sufficient, since a modify-before-its-create ordering bug only matters
  within one file's/one repo's history, never across repos.
- Repos process in parallel; a slow repo (e.g., mid-`git gc`) doesn't stall
  unrelated ones. This is the concurrency win per-dir repos were meant to
  provide, realized at the distribution layer instead of the local
  single-process one (where [git-backend-plan.md](../git-backend-plan.md)
  still correctly requires one lock per process today, since there's no
  natural parallelism to exploit locally with one consumer thread already).

## Idempotency — free, not built

At-least-once delivery (durable queue, manual ack after successful commit,
redelivery on crash-before-ack) needs a dedup mechanism in most systems. Not
here: **a `git commit` of already-committed content is a no-op** (empty
diff → nothing to commit, per [git-backend-plan.md](../git-backend-plan.md)'s
"Modified handler" design). Redelivering the same event after a central-writer
crash and restart is safe without any additional idempotency key — the
commit function itself absorbs the duplicate. This only holds if commits are
content-keyed rather than event-count-keyed; do not build a design that
commits "because an event arrived" independent of whether the content
actually differs from `HEAD`.

## Failure modes

| Failure | Effect |
|---|---|
| Central writer down | Events queue durably at the broker; no data loss, audit trail lags until it's back |
| Edge server down | That server's local changes simply don't happen; nothing to reconcile, no local state to lose (it never had a local git mirror) |
| Network partition, edge ↔ broker | Edge server's watcher keeps running, events buffer... **where?** See "Open question" below |
| Redelivery after crash | Idempotent by construction, per above — no special handling needed |

## Open question — edge-side buffering during a broker outage

An edge server with no reachable broker has nowhere to durably hold events
it detects locally — there's no local git mirror to fall back to (that's the
whole point of this topology) and no local queue either, unless one is
added. Two options, not resolved here:

1. **Accept event loss during a broker outage**, relying on
   `sync_source_status`-style reconciliation (a full re-scan against the
   edge server's local filesystem) once connectivity returns, similar to
   how [git-backend-plan.md](../git-backend-plan.md) already accepts a
   restart-only reconciliation story locally.
2. **Add a small local durable buffer per edge server** (e.g., a local
   append-only file or lightweight embedded queue) that drains once the
   broker is reachable again — more robust, more moving parts, and starts to
   resemble the "resilience/offline-first" direction
   [cowork-enterprise-plan.md](../cowork-enterprise-plan.md) explicitly
   deferred as a separate driver, not this one.

Recommend (1) for the initial build — it's consistent with this topology's
"scale, not resilience" positioning — and revisit (2) only if broker outages
turn out to be frequent enough in practice to matter.

## Files

| File | Change |
|---|---|
| `vcs/workers/remote/` (new) | edge-side publisher: watcher → serialize via `event_to_dict` → publish, no local consumer |
| `vcs/workers/central/` (new) | central-writer: per-repo queue consumers, each calling into `git_store` under that repo's lock |
| [rabbitmq-migration.md](../rabbitmq-migration.md) | update routing-key section to specify per-repo binding keys, not just `source`/`config` |

## Tests

- Integration: two simulated edge publishers writing to different repos
  concurrently — assert both commit correctly, no cross-repo lock
  contention, no reordering within either repo's history.
- Redelivery: force a central-writer restart mid-batch (kill before ack),
  assert the redelivered event produces no duplicate commit.

## Verification

Live, once a broker is available in the dev environment: run two edge
processes against two different source directories, publish overlapping
bursts of events, confirm `git log` on each resulting repo shows correct,
non-interleaved history, and that killing the central writer mid-flight and
restarting it against the same durable queues resumes cleanly with no
duplicate or lost commits.

## Not done here

- Edge-side durable buffering (deferred, see "Open question").
- Broker deployment/HA itself (RabbitMQ clustering, etc.) — an operational
  concern, not a design one for this codebase.
- The offline-first (git federation) and data-residency (metadata-only)
  distribution directions — deliberately separate specs, only written if a
  concrete driver picks one over this default, per
  [cowork-enterprise-plan.md](../cowork-enterprise-plan.md).
