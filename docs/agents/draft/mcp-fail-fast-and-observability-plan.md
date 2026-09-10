# Draft — MCP fail-fast + observability

Status: **done 2026-09-10**. Budgets settled (see Part 1); shipped as
spec [037](../../specs/037-git-subprocess-timeout-and-stdin.md) (Parts
1+2) and [038](../../specs/038-mcp-observability-and-bounded-waits.md)
(Parts 3-6). Written after
spec 036 (issue #29) bounded the
mirror-repo `FileLock` at 30s, and a live retest still couldn't explain a
`write_file` call that ran past 30s and had to be cancelled by hand - with
**nothing in any log** to check afterwards. The audit below is what that
investigation turned up: 036 fixed one unbounded wait, but the same call
path still has several more, and no way to see any of them.

Two clocks, same as the shipping draft's framing:

1. **Fail fast** - when something is stuck, does the call give up on its
   own, with a bound the caller can reason about?
2. **See it fail** - when it does, is there any record of *where*?

## Part 1 — every `git` subprocess call is unbounded

**All 24 `subprocess.run(["git", ...])` calls in
[git_store.py](../../src/vcs/services/git_store.py) omit `timeout=`.** A
hung `git` blocks forever.

This matters more than the `FileLock` bug spec 036 just fixed, because it
sits *inside* the lock. `write()` runs up to six sequential git calls -
`hash-object`, `update-index`, `write-tree`, `rev-parse`, `commit-tree`,
`update-ref` - under a single `_lock_for(repo_path)`. One stuck call holds
that lock for as long as it stays stuck, and every other process then
queues behind it and burns its own full 30s before giving up. That is the
compounding mechanism that turns one stall into a multi-minute cascade
across concurrent MCP sessions - and it's consistent with the
past-30s hang that prompted this audit, though not proven to be its cause
(nothing was logged, see Part 3).

**The timeout budget has to be coherent with the lock timeout**, which is
the part worth deciding deliberately rather than picking a number per
call. Today's `LOCK_TIMEOUT` is 30s: that is how long *a waiter* is
willing to wait. So the *holder* must never be able to hold it longer
than that, or the two bounds contradict each other. Six calls at 30s each
would allow a 180s hold against a 30s wait - incoherent. Invariant:

> worst-case lock hold (sum of git timeouts in the longest locked
> sequence) ≤ `LOCK_TIMEOUT` ≤ total call budget

### Decided budget: 45-60s total per MCP tool call

Working backwards from that ceiling. What the *synchronous MCP path*
actually runs (note it never calls `write()` - that six-call sequence
runs in the **daemon**, and is what the MCP path waits *behind*):

| Step | Git calls | Budget |
| --- | --- | --- |
| Wait for the lock (`LOCK_TIMEOUT`) | - | **30s** |
| `init_repo()` under the lock (`init` + 2× `config`) | 3 | 12s |
| `head_rev()` (unlocked `git log`) | 1 | 4s |
| `_set_actor_hint()` DB connect (Part 4) | - | **2s** |
| **Worst case total** | | **48s** |

- **Per-git-call timeout: 4s.** The longest locked sequence is
  `write()`/`move()` at six calls → 6 × 4 = **24s**, against a 30s lock
  timeout. The 6s margin is the point, not slack: at exactly 6 × 5 = 30s
  the two bounds are equal, so a waiter that starts waiting just as a
  maximally-slow-but-legitimate holder begins would time out at the same
  instant the holder finishes - a coin flip. With 4s the waiter always
  outlasts a holder that is genuinely working, and only gives up on one
  that is genuinely stuck. Still generous for local plumbing commands on
  a small bare repo (normally single-digit ms); worth one check against a
  cold/AV-scanned Windows box, since `git init` on a first-ever repo is
  the slowest observed.
- **`LOCK_TIMEOUT` stays 30s** - the value spec 036 already shipped needs
  no revision at this budget, which is a point in favour of the wider
  ceiling.

Excluded from this budget: `ctx.elicit()` (Part 5) - human latency, not
machine latency, and no human reliably answers a prompt inside 45s.
Flagged rather than assumed; see Part 5.

Unresolved: what a timeout should *do*. `subprocess.TimeoutExpired` kills
the child, but a `write()` killed between `update-index` and
`commit-tree` leaves exactly the staged-but-uncommitted index that spec
027's `reset_stale_index()` already exists to clean up at boot - so the
recovery path is already built, and the failure is not new in kind, only
newly reachable without a force-kill. Worth stating explicitly in the
spec rather than discovering later.

## Part 2 — git subprocesses inherit the MCP server's stdin

23 of those 24 calls pass neither `stdin=` nor `input=` (only
`_hash_object` sets a pipe, via `input=content`), so the child inherits
the parent's stdin.

In the MCP server that stdin **is the JSON-RPC pipe from the client**.
If git ever decides to prompt - a credential helper, an askpass fallback,
`core.editor`, a "terminal prompts disabled" path that isn't actually
disabled - it blocks forever reading a stream that will never contain an
answer, *and* any bytes it does consume are stolen from the protocol
stream. A hang plus a corrupted transport, from one inherited file
descriptor.

The daemon is immune by construction: `_spawn()` already passes
`stdin=subprocess.DEVNULL` ([daemon.py](../../src/app/cli/daemon.py)).
The MCP server never got the same treatment because nothing in
`git_store` was written with "my stdin is a protocol channel" in mind.

**Fix direction:** `stdin=subprocess.DEVNULL` on every git call that
doesn't already pass `input=`, plus `GIT_TERMINAL_PROMPT=0` in the
subprocess env so git *fails* instead of trying to prompt at all. Both
are belt-and-braces on purpose - DEVNULL alone turns a prompt into an EOF
read (git may then fail confusingly rather than cleanly), and the env var
alone doesn't cover a helper that reads the tty directly.

## Part 3 — the MCP server logs nothing, anywhere

`main()` is `mcp.run(transport='stdio')` - **it never calls
`setup_logger()`**, and `git_store.py`, `app/mcp/server.py` and
`app/mcp/guardrail.py` contain **zero** log statements between them. The
entire MCP request path is silent. That is why "check the log" after the
hang found nothing: `data/ctx.log` only contains the daemon's lines,
because `_spawn()` redirects the *daemon's* stdout/stderr into that file.
The MCP server has no equivalent - its stderr goes to whatever the client
does with it, which is nothing durable.

Design constraint that makes this non-trivial: **stdout is the protocol**.
Anything logged to stdout corrupts the JSON-RPC stream. `setup_logger()`
uses `logging.basicConfig()`, whose default stream is stderr, so it is
safe on that axis - but stderr is still invisible after the fact. So the
MCP server needs an explicit **file** handler (`data/mcp.log`, alongside
`ctx.log`), not just a `setup_logger()` call.

What to log, chosen so a future hang is diagnosable from the file alone:

- Tool entry/exit with elapsed time, per call.
- Lock acquisition: before waiting, and after acquiring with the wait
  duration - a slow acquire is the single most useful signal here.
- Each git invocation with its duration, or its timeout/failure.
- Every currently-silent swallow (Part 4).

Deliberately *not* logging tool arguments or file contents by default -
these are the user's context sources, and this file would otherwise
become a plaintext copy of watched documents. Paths only.

## Part 4 — best-effort paths wait too long and fail invisibly

**`_set_actor_hint()`** ([server.py:66](../../src/app/mcp/server.py))
opens a connection through `DBHandler.from_url()`, inheriting its
**30s** SQLite busy timeout (issue #20/spec 016) - for bookkeeping whose
own docstring says "an MCP call must never fail because hint bookkeeping
couldn't complete". Waiting 30 seconds for something you are explicitly
willing to skip is the wrong trade: under real DB contention it adds up
to 30s to a call that then succeeds anyway, and because the failure is
swallowed (`except (sqlite3.Error, TypeError): pass`, no log) there is no
signal at all - not to the caller, not to a file. A permanently broken DB
stays invisible forever, and every write silently loses actor
attribution.

Fix direction: give this path its own short timeout (1-2s, since the
whole point is that skipping is acceptable) and log at WARNING when it
gives up. `DBHandler.from_url()` already takes a `timeout` parameter, so
this needs no new plumbing.

**`ensure_scope()`'s catch-all**
([guardrail.py:53](../../src/app/mcp/guardrail.py)) -
`except Exception: return ScopeGrant(False)` catches *everything*,
including genuine programming errors, and reports all of it as an
ordinary "denied". A bug in the elicitation path and a user clicking
"no" are indistinguishable to the caller and leave no trace. Narrowing
the catch is a behavior change worth its own thought (fail-closed is the
right default for a security boundary, and that shouldn't be lost) - but
logging the exception before returning is free and non-negotiable.

## Part 5 — `ctx.elicit()` has no timeout

Same function, the `await ctx.elicit(...)` call: no bound at all. A
client that never renders the prompt, or a user who walks away, holds the
tool call open indefinitely. Fail-closed already covers decline/cancel;
this is the case where no answer ever arrives.

Fix direction: wrap in `asyncio.wait_for()`, treating expiry as a
decline, consistent with the existing fail-closed contract.

**Decided: 120s.** This is the one bound here measuring human latency
rather than machine latency, so it sits outside the 45-60s call budget
entirely. 120s over 60s because a person actually reading the path and
deciding whether to widen scope can reasonably take longer than a minute
- and in the failure case this really protects against (a client that
never renders the prompt at all) nobody is watching either number, so the
shorter one buys nothing.

## Part 6 — `grant.commit()` runs unprotected, after the write

Every tool ends with `grant.commit()` *outside* its `try` block
([server.py](../../src/app/mcp/server.py), all five tools). That call
runs `add_sources()`, which re-reads and rewrites `config.yaml`. If it
raises, the exception escapes as a transport-level error **even though
the file write already succeeded** - the caller is told the operation
failed while the disk says otherwise, and an agent acting on that will
retry a write that already happened.

Fix direction: wrap it, and on failure return success for the operation
that genuinely succeeded while reporting that scope persistence didn't
(a distinct field, not a silent swallow - the next call will re-prompt
for approval, and the caller should know why).

## Suggested spec grouping

Two specs, not six - the findings cluster cleanly by file and by the
change they need:

1. **`git_store` subprocess hardening** (Parts 1 + 2) - per-call
   `timeout=`, budget coherent with `LOCK_TIMEOUT`, `stdin=DEVNULL` +
   `GIT_TERMINAL_PROMPT=0`. Highest impact, and the two changes touch the
   same 24 call sites, so splitting them means editing every one twice.
2. **MCP observability + bounded best-effort paths** (Parts 3 + 4) -
   file logging for the MCP server, plus the two silent swallows that
   become useful the moment there's somewhere to write them.

Parts 5 and 6 are smaller and independent; fold into spec 2 only if it
stays small, otherwise leave for a follow-up.

## Non-goals / open questions

- Whether the daemon's own git calls want the same timeout - they do by
  construction (same `git_store` functions), but the daemon has no
  synchronous caller waiting, and `consumer_worker`'s existing
  handler-failure boundary already logs-and-continues. No separate work,
  just worth confirming the failure shape there is acceptable.
- Settled: 45-60s total call budget → 4s per git call, `LOCK_TIMEOUT`
  unchanged at 30s, 2s actor hint, 48s worst case; `elicit` 120s,
  deliberately outside that budget because it measures human latency,
  not machine latency.
- Whether `data/mcp.log` should rotate. `ctx.log` doesn't today either -
  same pre-existing question, not made worse by this, but a per-call log
  line grows faster than the daemon's per-event ones.
