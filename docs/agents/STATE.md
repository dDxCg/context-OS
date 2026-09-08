# State — what's built, and why (design trail)

Consolidates `cli-plan.md`, `competitive-landscape.md`, `cowork-enterprise-plan.md`,
`dir-events-plan.md`, `git-backend-plan.md`, `pubsub-plan.md`, `rabbitmq-migration.md`
(retired 2026-09-09 — all were stale: several said "planned, not started" while fully
implemented). For current architecture, read [`ARCHITECTURE.md`](../../ARCHITECTURE.md) —
this file is the *why*, not the *what*; it exists so the reasoning behind decisions already
made isn't lost. Full issue history: [issues.md](issues.md). Still-open work:
[FUTURE.md](FUTURE.md).

## Storage: git mirror, not a hand-rolled blob store

Original design was a SQLite `versions` table + a flat `BLOB_DIR` of `{hash}.blob` files —
a weaker reimplementation of what a git object store already does (content-addressed
storage, dedup, diffing, commit graph as history). Swapped to one git repo per watch
target (`GIT_REPO_DIR`, specs 003-006), not the source directory itself — least-privilege
by scope, and sources are scattered/file-granular so a single working tree per source
doesn't fit.

Shelling out to the real `git` binary via `subprocess`, not `dulwich` — `git mv`/
`git rm -r`/porcelain diff are exactly the needed primitives, well-tested, avoid
reimplementing rename/tree semantics. Cost: `git` becomes a runtime prerequisite
(documented in README). Revisit only if the project ever ships as a single packaged
executable with no external binary assumption.

The `locations` table identity layer (`st_ino`/`st_dev` → `context_id`) is kept — git
tracks by path, not inode, so it's still what lets `moved_handle` find which mirror path
to `git mv`.

This swap structurally fixed several issues for free (git commit on unchanged content is
a no-op; `git mv`/`git rm -r` handle a file or a whole directory with the same call,
no `is_dir` branching needed) — see [issues.md](issues.md) #17/#18 (moot) and #21/#22
(fixed) for the detail.

**Single-writer lock is mandatory.** `.git/index.lock` doesn't arbitrate concurrent
writers the way SQLite's WAL does — a second committer fails outright rather than
queuing. `git_store.py` holds one cross-process `filelock.FileLock` per mirror repo
(spec 012) around every write.

## Directory events: fixed by the storage swap, not a bespoke SQL design

The original plan for #21/#22 (subtree-prefix `UPDATE ... WHERE substr(...)` matching,
avoiding `is_dir` since Windows can't report it on a delete) was superseded before it was
ever built — the git-backend migration made the mirror-side directory check
(`Path.is_dir()` on the *mirror* path, not the event's `is_dir`) the actual fix, structurally
immune to the Windows/Linux emission-order split that made the original SQL design
necessary. Confirmed live and via re-audit (2026-09-09): `deleted_handle`/`moved_handle`
in `versioning.py` do exactly this today.

## Pub/sub: an AMQP-shaped local bus, not ad-hoc routing

`LocalRuntime._route` used to do an `isinstance` demux and hand-pick a queue per event —
that coupling caused issues.md #1 (one `STOP`, two blocked readers) and a later
queue-aliasing regression. Replaced with `vcs/workers/bus.py`'s `LocalEventBus`: producers
publish once with a routing key (`source`/`config` — coarse, category not verb), consumers
bind `source.#`/`config.#` (`#` matches zero-or-more words, AMQP topic-exchange semantics),
`bus.close()` fans `STOP` out to every subscription so shutdown can't drift out of sync
with the consumer set again.

Shaped this way on purpose, for a future RabbitMQ swap (see [FUTURE.md](FUTURE.md)) — not
because RabbitMQ is needed today. `event_to_dict`/`event_from_dict` (wire serialization,
tested) exist for the same reason, unused locally since events pass by reference.

**Deliberate divergence from AMQP, worth remembering:** the scope predicate
(`Subscription.where`) is evaluated by the bus *during fanout, on the publishing thread*,
not inside the consumer thread — because `_scope_cache` is lock-free by design, depending
on only watchdog's single dispatcher thread touching it. A real broker has no fanout-time
predicate; migrating this becomes a consumer-side check, needing its own cache
synchronization.

**Constraints that must survive any future broker swap:** delivery must stay async (never
run a handler on watchdog's dispatcher thread — it holds `BaseObserver._lock`, and a
handler doing DB work there deadlocks against `ConfigConsumer.watcher.reconcile()`, which
needs that same lock); mailboxes must stay unbounded for the identical reason; a failing
predicate must never escape `publish()` (the publisher *is* the dispatcher thread).

## Process supervision: detached process + PID file, not a service manager

`ctx daemon start/stop/status` (spec 019) — a systemd unit or Docker healthcheck would be
over-engineering for a bare dev-machine tool. Cross-platform stop is the one real
complexity: POSIX gets a plain `SIGTERM` (spec 017's handler does a clean shutdown);
Windows needs `CTRL_BREAK_EVENT` against the process's group, since `os.kill(pid, SIGTERM)`
on Windows maps straight to `TerminateProcess` — abrupt, no cleanup, defeating spec 017
entirely.

Scoped to the watch daemon only. The MCP server is spawned per-need by an MCP client
(`fastmcp.json`), not something this project backgrounds itself; the HTTP API is a
request-driven server most operators already run via `uvicorn`/a reverse proxy like any
other web service.

## Actor attribution: git commit author, cross-process hint handoff

Every commit's author reflects who made the change (spec 007: `_resolve_actor` maps
`event.actor` to a git author string, `unknown:filesystem` default for a bare edit with no
upstream identity — deliberately honest, not narrowed away, since the watcher's broader
capture surface than tool-call interception is a real strength worth keeping visible).

MCP-triggered edits (spec 013) close the harder half: the MCP server and the daemon are
**separate OS processes**, so an in-memory dict can't hand off "this next filesystem event
is from agent X" — solved with a short-lived, path-keyed `pending_actor_hints` SQLite
table (both processes already share the DB), TTL'd so a later unrelated edit never inherits
a stale hint.

## Competitive positioning (why this project's shape is what it is)

Surveyed against cachebro, codebase-memory-mcp, knowledge-base-server, palinode, GitAgent,
and Claude Code's own file-checkpoint system (2026, research note, not re-run since —
treat as a snapshot). Findings that shaped design decisions above:

- No comparable project does real multi-version storage + rollback for **non-git-backed**
  content — palinode/GitAgent get versioning "for free" by requiring the source to already
  be a git repo; chrono-ctx targets arbitrary sources (exported docs, non-repo folders)
  where that assumption doesn't hold. This is the core differentiator the git-mirror design
  serves.
- No comparable MCP server has interactive, scope-based access control —
  codebase-memory-mcp's `CBM_ALLOWED_ROOT` is a static, coarse, startup-time root; this
  project's elicitation-based guardrail (request scope, human approves, fail-closed on
  refusal/no-elicitation-support) is more sophisticated than anything found.
- Claude Code's `/rewind` validates one gap chrono-ctx still doesn't answer: **multi-file,
  session-scoped atomic rollback**. See [FUTURE.md](FUTURE.md) — the single biggest
  unbuilt product gap.
