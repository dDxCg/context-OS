# 013 — Pending actor hints for MCP-triggered edits

Status: implemented

## Context

[issues.md #23](../agents/issues.md): MCP write tools deliberately do plain
filesystem I/O only — no direct call into `versioning.py`, since the
watcher already tracks every change and calling both would double-commit.
The cost: the watcher's event carries no actor identity (watchdog gives
none), so every MCP-triggered edit lands in git as `unknown:filesystem`,
indistinguishable from a human editor save.

**The MCP server and the daemon are separate OS processes** (confirmed
architecture, ARCHITECTURE.md §3) — an in-memory dict can't bridge them, the
same constraint spec 012 hit for the repo lock. The fix there was a
cross-process file lock; here it's a small cross-process hint store, reusing
the SQLite DB both processes already share for identity (`contexts`/
`locations`).

## Design

A `pending_actor_hints` table: `(location PRIMARY KEY, actor, expires_at)`.
Before its filesystem I/O, an MCP write tool records "the next event for
this path is actor X, valid for N seconds". The daemon's event consumer,
right before dispatching to `versioning.py`, looks up and **consumes**
(reads then deletes) any hint for the event's path and fills in
`event.actor` if the event didn't already carry one.

A missed or expired hint degrades to today's behavior
(`unknown:filesystem`) — never a hard failure. `set_hint` on the MCP side is
best-effort: if the DB/table isn't there yet (MCP started before the daemon
ever ran once, so schema was never applied), it logs and moves on rather
than failing the write.

TTL default 5s — comfortably above the watcher's 0.5s debounce plus normal
dispatch latency, short enough that a later unrelated edit of the same path
never inherits a stale hint.

## Scope

**In**
- `data/schema.sql`: `pending_actor_hints` table.
- `vcs/services/actor_hints.py`: `set_hint`/`consume_hint`.
- `vcs/workers/local/local_consumer.py`: `LocalConsumer.handle` consumes a
  hint for `event.src` (and `event.dst` for a `MovedEvent`, since either
  could be what the resulting watcher event keys on) when `event.actor` is
  `None`.
- `app/mcp/server.py`: `write_file`/`create_file`/`delete_file` set a hint
  for `path`; `move_file` sets hints for both `src` and `dst`. Actor label:
  `f"agent:{ctx.session_id}"`.

**Out**
- CLI actor capture — no CLI command currently writes content through the
  watcher path (`ctx rollback` already attributes its own git commit
  directly via `git_store`, no watcher round-trip needed for attribution).
  Nothing to wire.
- Config-source-add-triggered scans (`ConfigConsumer` replaying
  `created_handle`/`deleted_handle` for a config diff) — a rarer path,
  already unattributed before this spec, left as-is.

## Acceptance criteria

- AC-1. `set_hint(db, location, actor)` then `consume_hint(db, location)`
  returns `actor`.
- AC-2. `consume_hint` is destructive: a second call for the same location
  returns `None`.
- AC-3. `consume_hint` on a location with no hint returns `None`.
- AC-4. `consume_hint` past `ttl_seconds` returns `None` (and still deletes
  the stale row).
- AC-5. `LocalConsumer.handle` fills in `event.actor` from a pending hint
  when the event's own `actor` is `None`, before dispatching to
  `versioning.py` — the resulting git commit's author reflects the hint.
- AC-6. An MCP `write_file` call sets a pending hint for `path` labeled
  `agent:{session_id}` before doing the real filesystem write.

## Error cases

- EC-1. `set_hint` when the DB/table isn't available yet does not raise -
  the filesystem write still proceeds.
- EC-2. `LocalConsumer.handle` with no pending hint and `event.actor` already
  `None` dispatches unchanged (falls through to `_resolve_actor`'s existing
  `unknown:filesystem` default, spec 007 behavior untouched).

## Contracts

```python
# vcs/services/actor_hints.py
def set_hint(db_handler: DBHandler, location: str, actor: str, ttl_seconds: float = 5.0) -> None: ...
def consume_hint(db_handler: DBHandler, location: str) -> str | None: ...
```

## Non-goals / open questions

- None outstanding.
