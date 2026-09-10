# 038 — MCP server logging, and bounds on the paths that fail invisibly

Status: implemented

## Context

From [mcp-fail-fast-and-observability-plan.md](../agents/draft/mcp-fail-fast-and-observability-plan.md)
Parts 3-6. Spec 036 bounded the mirror-repo lock, spec 037 bounded every
git subprocess - but when the hang that started this investigation was
reported, **there was nothing to check**: the MCP server writes no log,
anywhere.

`main()` is `mcp.run(transport='stdio')` - it never calls
`setup_logger()`, and `git_store.py`, `app/mcp/server.py` and
`app/mcp/guardrail.py` contain zero log statements between them.
`data/ctx.log` only holds the *daemon's* lines, because `_spawn()`
redirects the daemon's stdout/stderr into that file; the MCP server has
no equivalent.

The constraint that makes this non-trivial: **stdout is the protocol**.
Anything written there corrupts the JSON-RPC stream. `setup_logger()` uses
`logging.basicConfig()`, whose default stream is stderr, so it is safe on
that axis - but stderr from a stdio MCP server goes wherever the client
puts it, which is nowhere durable. So this needs an explicit *file*
handler, not just a `setup_logger()` call.

Alongside that, three paths that currently fail or wait invisibly:

- `_set_actor_hint()` inherits `DBHandler.from_url()`'s **30s** SQLite
  busy timeout for bookkeeping its own docstring calls best-effort, then
  swallows any failure with a bare `pass`. Waiting 30s for something you
  are explicitly willing to skip is the wrong trade, and the silence means
  a permanently broken DB - every write silently losing actor
  attribution - stays invisible forever.
- `ensure_scope()`'s `except Exception: return ScopeGrant(False)` catches
  everything, including genuine bugs, and reports all of it as an ordinary
  "denied" - indistinguishable from a user clicking no.
- `await ctx.elicit(...)` has no bound at all: a client that never renders
  the prompt holds the call open forever.

And one correctness bug found in the same audit: `grant.commit()` runs
*outside* every tool's `try` block, after the operation succeeded. It
rewrites `config.yaml`; if it raises, the exception escapes as a
transport-level error **even though the file write already landed** - the
caller is told it failed while the disk says otherwise, and an agent
acting on that will retry a write that already happened.

## Scope

**In**
- `app/mcp/server.py`'s `main()`: configure logging to a file
  (`data/mcp.log`, resolved the same `PROJECT_ROOT`-anchored way as
  `ctx.log`) before `mcp.run()`. Never to stdout.
- Log lines chosen so a future hang is diagnosable from the file alone:
  tool entry/exit with elapsed time, and the two currently-silent
  swallows below. Paths only - never tool arguments or file contents,
  which are the user's context sources and would turn this file into a
  plaintext copy of watched documents.
- `_set_actor_hint()`: its own short timeout (`HINT_DB_TIMEOUT = 2.0`,
  passed to the existing `DBHandler.from_url(..., timeout=)` parameter),
  and a WARNING log when it gives up instead of a bare `pass`.
- `ensure_scope()`: log the exception before returning `ScopeGrant(False)`
  (`logging.exception`), so a bug is distinguishable from a decline in the
  file even though the caller-facing behavior is deliberately unchanged.
- `ensure_scope()`: bound `ctx.elicit()` with
  `asyncio.wait_for(..., timeout=ELICIT_TIMEOUT)` where
  `ELICIT_TIMEOUT = 120.0`, treating expiry as a decline - consistent with
  the existing fail-closed contract, and logged.
- Every tool: move `grant.commit()` inside a guarded block so a failure to
  persist scope can't report the *operation* as failed when it succeeded.

**Out**
- Narrowing `ensure_scope()`'s catch-all beyond adding the log. Failing
  closed is the right default for a security boundary and shouldn't be
  weakened as a side effect of an observability change - a separate
  decision if ever wanted.
- Log rotation for `data/mcp.log`. `ctx.log` doesn't rotate either; same
  pre-existing question, not made worse here, but worth noting that a
  per-call line grows faster than the daemon's per-event ones.
- `subprocess.TimeoutExpired` (spec 037) surfacing through the MCP tools:
  it is **not** an `OSError` subclass, so it is not covered by
  `IO_ERRORS` - see AC-7.

## Acceptance criteria

- AC-1. `main()` configures a file handler writing to `data/mcp.log`, and
  no handler writes to `stdout` (which carries the JSON-RPC protocol).
- AC-2. A successful tool call writes an entry and an exit line including
  elapsed time; the file contains the path but never the content written.
- AC-3. `_set_actor_hint()` opens its connection with a 2s timeout, not
  `DBHandler.from_url()`'s 30s default.
- AC-4. When `_set_actor_hint()` fails, it logs at WARNING and the tool
  call still succeeds - the best-effort contract is unchanged, only its
  silence is.
- AC-5. `ensure_scope()` logs the exception when its catch-all fires, and
  still returns a falsy grant (fail-closed behavior unchanged).
- AC-6. `ctx.elicit()` that never resolves is abandoned after
  `ELICIT_TIMEOUT` and treated as a decline, logged - not left open
  indefinitely.
- AC-7. A tool call whose git work raises `subprocess.TimeoutExpired`
  (spec 037) returns a structured `{"status": "error", ...}` rather than
  escaping as a transport-level error - `IO_ERRORS` gains it explicitly,
  since `TimeoutExpired` derives from `SubprocessError`, not `OSError`.
- AC-8. If `grant.commit()` fails after a successful operation, the tool
  still reports the operation as successful, with the scope-persistence
  failure surfaced as a distinct field rather than swallowed - the next
  call re-prompting for approval is then explainable.

## Error cases

- EC-1. `data/mcp.log` not writable (read-only dir, permissions): logging
  setup must not prevent the server from starting - a server that refuses
  to run because it can't log is strictly worse than one that runs
  unlogged.
- EC-2. `_set_actor_hint()`'s 2s timeout expiring is a normal, expected
  outcome under contention, not an error the caller ever sees - WARNING,
  not ERROR, and never propagated.

## Contracts

```python
# app/mcp/server.py
HINT_DB_TIMEOUT: float = 2.0
IO_ERRORS = (OSError, UnicodeError, shutil.Error, subprocess.TimeoutExpired)
def main() -> None: ...  # now configures file logging first

# app/mcp/guardrail.py
ELICIT_TIMEOUT: float = 120.0
async def ensure_scope(ctx: Context, path: str) -> ScopeGrant: ...  # same signature
```

## Non-goals / open questions

- Whether the daemon should also log to `data/mcp.log`-style structured
  entries - out of scope, it already logs adequately via `log_enabled`
  plus the bus/consumer failure boundaries.
