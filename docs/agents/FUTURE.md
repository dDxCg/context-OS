# Future — unbuilt work, ranked

Consolidated 2026-09-09 from the retired plan docs (see [STATE.md](STATE.md)) plus
`ARCHITECTURE.md` §8's Tier-3-era gap list. Nothing here has a spec yet — a spec gets
written only once someone is about to implement it (`AGENTS.md`'s rule).

## 1. Multi-file / session-scoped atomic rollback — biggest gap

`ctx rollback <file> -v N` is per-file only. An agent session realistically touches
several context files per run; there's no way to undo "everything session/config-diff X
did" as one unit — the gap Claude Code's own `/rewind` (atomic across every file touched
since a checkpoint) highlights by comparison. Needs a run/session id threaded through
ingestion so a batch, not just one file, can be a rollback target. Design this into
`audit.py` alongside whatever triggers it, not bolted on after — retrofitting a session id
onto an already-shipped per-file model is more painful later.

## 2. Optimistic-concurrency gate beyond `rollback`

`git_store.write_with_check(expected_rev=...)` (spec 012) exists but is wired into exactly
one caller: `rollback_source`. MCP write tools (`write_file`/`create_file`/...) and general
CLI writes still do plain filesystem I/O with no expected-revision check — two concurrent
writers to the same path (two agent sessions, or MCP racing a human edit) is a silent
lost-update, not a detected conflict. This was flagged early as "conflict-ux" — turned out
to need no conflict-marker translation layer (the single-writer lock means there's
structurally never a git *merge* conflict, only a lost-update race), just this gate wired
more broadly.

## 3. Non-technical approval channel

The MCP guardrail's elicitation-based approval only renders inside an MCP client — a
non-technical stakeholder has no way to see or approve a scope request without one. At
minimum a CLI command they could be walked through; more realistically a
notification/web surface outside this repo's current scope, depending on `audit.py`/
`guardrail.py` exposing the right primitives to be driven from outside a single MCP
session.

## 4. Retention / GC for the git mirror

No `ctx gc` escape hatch exists. Today's only housekeeping is git's own `gc.auto` per
commit (zero extra code) — fine at current scale. Add a manual `ctx gc [source]` command,
routed through the same writer lock as any other write, once mirror-repo size becomes an
actual operational concern (not yet observed).

## 5. Self-heal for a dropped watcher event

The only reconciliation against a missed OS filesystem event is `sync_source_status` at
process restart. No periodic or on-read reconcile exists, so a dropped event (rare, but
possible — `cachebro` sidesteps this by hash-verifying on every read instead of trusting
the watcher) leaves `locations` wrong until the next restart. Not yet a live-observed
problem; worth building only if it becomes one.

## 6. RabbitMQ central-writer distribution — deferred pending a real driver

Full topology already designed, nothing built: one topic exchange (`vcs.events`), two
durable queues bound `source.#`/`config.#`, future 3rd-party source adapters (webhook
receiver, polling service — already stubbed in `config.example.yaml`) publishing onto the
same exchange with their own routing keys. `event_to_dict`/`event_from_dict` (tested) and
the bus's AMQP-shaped routing (see [STATE.md](STATE.md)) were built anticipating exactly
this boundary. Still missing: wire codec/schema-version headers, connection/channel
lifecycle + reconnect, ack/nack semantics (likely grows the `EventBroker` interface), and
where the scope predicate runs once there's no shared-process cache to lean on (see
STATE.md's divergence note — becomes a consumer-side check, not a broker feature).

**The distribution design on top of that (consolidated from a stray
`docs/specs/central-writer-distribution.md`, never numbered, never built):**

```
Edge server A ─┐
Edge server B ─┼─► vcs.events (topic exchange) ─► central-writer queue(s) ─► git mirror repos
Edge server N ─┘        source.# / config.#
```

Edge servers run the watcher only — no local git mirror, no local consumer doing
`versioning.py` work. They publish events (`event_to_dict`'s shape, extended with `actor`)
onto the exchange and stop. All writes happen at exactly one logical place, sidestepping
distributed git entirely (no remotes, no push/pull, no merge) by construction.

- **Partitioning:** one durable queue *per repo* (routing key derived from the source
  path, e.g. `source.<repo-id>`), each with its own consumer thread/process — a single
  global consumer would serialize writes across every repo and throw away the concurrency
  per-directory mirror repos were meant to buy. Ordering only needs to hold within a queue
  (per repo), never across repos.
- **Idempotency is free, not built:** a `git commit` of already-committed content is a
  no-op, so at-least-once delivery needs no dedup key — a redelivered event after a
  crash-before-ack just produces the same no-op commit. Only holds if commits stay
  content-keyed, never "commit because an event arrived" independent of whether content
  actually differs from `HEAD`.
- **Open question, not resolved:** an edge server with no reachable broker has nowhere to
  durably hold events it detects locally (no local git mirror, no local queue, unless one
  is added). Two options: (1) accept loss during an outage, rely on a
  `sync_source_status`-style full rescan on reconnect — recommended for an initial build,
  consistent with a "scale, not resilience" positioning; or (2) add a small local durable
  buffer per edge server that drains once reachable — more robust, more moving parts,
  starts to resemble the offline-first direction below.

**Do not start any of this without a concrete driver** — a real branch-office/multi-edge
requirement, or a data-residency regulation that makes today's single-process model
actually insufficient. Git federation (offline-first) and metadata-only central sync are
both real alternative directions, deliberately not chosen over this one without that
driver either.

## 7. Already tracked elsewhere — not duplicated here

See `ARCHITECTURE.md` §8 for: HTTP API per-caller scope (today: one shared `X-API-Key`),
no HTTP write endpoint for rollback (deliberate, matches the read-only API scoping), and
MCP-server/HTTP-API process supervision (deliberately out of `ctx daemon`'s scope — see
STATE.md). `config.yaml` write races between CLI/MCP/daemon are a known, accepted hazard,
documented in `get_config_diff`'s docstring — not repeated here.
