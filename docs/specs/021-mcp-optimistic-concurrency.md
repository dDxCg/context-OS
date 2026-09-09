# 021 — Optimistic-concurrency check on MCP `write_file`/`delete_file`

Status: implemented

## Context

[FUTURE.md #2](../agents/FUTURE.md): `git_store.write_with_check(expected_rev=...)` (spec
012) is wired into `rollback_source` and `rollback_session` (spec 020), but every MCP write
tool still does plain filesystem I/O with no expected-revision check. Two writers to the
same path — two agent sessions, or an agent racing a human's edit — is a silent lost-update:
whoever's filesystem write lands last wins, with no signal to the loser that their edit
never actually applied.

`read_file` already returns `version` (spec 008) for exactly this purpose — a caller that
reads a file, edits it, and writes it back already has the version its edit was based on.
This spec is the other half: let the caller assert that version at write time.

**Architectural limitation, stated up front, not hidden.** MCP write tools deliberately do
*not* call into `vcs.services.versioning`/`git_store` directly — the watcher commits
independently and asynchronously, after this tool call returns (see `server.py`'s module
docstring, and spec 013's actor-hint handoff for the same reason). So this check can only
compare against `current_version(path)` **at the moment the tool is called** — it cannot be
the same atomic check-and-commit `write_with_check` gives `rollback_source`/
`rollback_session`, where the check and the git write happen in one call. A second writer
racing in the window between this check and the watcher's later commit is not caught. This
narrows the lost-update window from "any time" to "the gap between an MCP call's version
check and the watcher's debounce-plus-dispatch latency" (sub-second in practice) — a real
reduction, not a complete fix, and documented as such.

## Design

Add an optional `expected_version: str | None = None` parameter to `write_file` and
`delete_file` — mirroring `write_with_check`'s own `expected_rev=None` skip-the-check
default, so existing callers that don't pass it see no behavior change. When provided, it's
checked against `current_version(path)` immediately after the scope grant and before any
filesystem I/O or actor-hint bookkeeping. A mismatch returns
`{"status": "conflict", "reason": ..., "current_version": <actual>}` — structured, like
every other failure path in this file, never an exception — and does **not** commit the
scope grant, same as the existing IO-error paths (a failed operation must never permanently
widen scope, per `ARCHITECTURE.md` §6.3).

**`create_file` and `move_file` are deliberately out of scope.** `create_file`'s conflict
shape is different — "did someone else already create this path" (no version to have read
beforehand) — and `move_file` is a location change, not a content edit against a version the
caller previously read. Both are real, separate problems; not the lost-update-on-existing-
content case this spec addresses. Noted in FUTURE.md as still open rather than silently
folded in here.

## Scope

**In**
- `app/mcp/server.py`: `expected_version` parameter on `write_file`/`delete_file`, checked
  against `current_version(path)` before I/O; `"conflict"` status shape.

**Out**
- `create_file`/`move_file` — see Design.
- CLI writes — no CLI command does an arbitrary content edit outside `rollback`/
  `rollback-session`, which already use `write_with_check` (specs 012/020). Nothing left to
  wire.
- Making the check atomic with the watcher's eventual commit — architecturally not possible
  without collapsing the MCP-tool/watcher split this project deliberately keeps (see
  Context); the narrowed race window is the accepted trade-off.

## Acceptance criteria

- AC-1. `write_file(path, content, expected_version=None)` behaves exactly as before —
  omitting the parameter skips the check entirely.
- AC-2. `write_file(path, content, expected_version=X)` where `X` matches
  `current_version(path)` succeeds normally.
- AC-3. `write_file(path, content, expected_version=X)` where `X` does **not** match
  `current_version(path)` returns `{"status": "conflict", "reason": ..., "current_version":
  <actual>}`, does not modify the file, and does not set a pending actor hint.
- AC-4. AC-1/AC-2/AC-3 hold identically for `delete_file`.
- AC-5. A conflict does not persist the scope grant — a subsequent call to the same
  out-of-scope-turned-approved path still elicits, exactly as an IO error already behaves
  (`test_failed_operation_does_not_persist_the_scope_grant`'s existing invariant).

## Error cases

None beyond AC-3/AC-4's conflict path — there is no separate error shape to enumerate; a
version mismatch *is* the error case this spec adds.

## Contracts

```python
# app/mcp/server.py
async def write_file(path: str, content: str, ctx: Context, expected_version: str | None = None): ...
async def delete_file(path: str, ctx: Context, expected_version: str | None = None): ...
```

Conflict response shape: `{"status": "conflict", "reason": str, "current_version": str | None}`

## Non-goals / open questions

None outstanding.
