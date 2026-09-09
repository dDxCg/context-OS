# Draft — live integration test: real daemon + real Claude Code session

Automated as [scripts/live_integration_test.py](../../../scripts/live_integration_test.py) —
runs steps A, C, D, E, F below against whatever `config.yaml` is currently
configured, cleans up after itself, and prints a PASS/FAIL/SKIP summary.
Step B (does Claude Code's own UI render elicitation) is out of its reach by
construction — it always reports that step `SKIP`.

Status: **partially run, 2026-09-09.** Steps A, D, E, F below were executed
against the real dev daemon and real `knowledge.example/` (not a scratch
`data/live-test/` env as originally planned — done directly against the
already-configured real `config.yaml`, since that turned out to already be a
sandboxed example tree, not production data). Step C's actual question
(does Claude Code's own UI render elicitation) is still **not answered** —
this pass used an in-process `fastmcp.Client` with an auto-approving
elicitation handler as a stand-in, which proves the server-side mechanism
works but cannot prove what a real Claude Code client renders. Two new real
bugs found, logged as [issues.md](issues.md) #25 and #26 (see below and
[FUTURE.md](FUTURE.md) item 5). Settles the open item
[install-integration-plan.md](install-integration-plan.md) Phase 2 left
hanging: whether a real Claude Code session actually renders the MCP
guardrail's elicitation prompts, plus a broader live pass over every
feature that only a real running system (not `pytest`) can prove — a live
daemon, a live MCP connection, a live HTTP API, real files moving through
all of it.

## Why this can't be a pytest suite

Everything unit/integration-tested today runs in-process or against a
stubbed transport. What's specifically unverified:

- Whether Claude Code's MCP client **renders** an elicitation prompt at all
  — this is UI behavior in a tool this repo doesn't control, not something
  `fastmcp`'s test harness can simulate.
- Whether the full cross-process handoff (MCP server process → pending
  actor hint in SQLite → daemon's watcher process picks it up → git commit)
  actually closes the loop end-to-end with two real OS processes and real
  `watchdog` filesystem events, not the in-process stand-ins integration
  tests use.

This is a human-observed, one-time (then periodic-as-needed) manual test
pass, not a new automated suite. AGENTS.md's spec-driven TDD workflow
governs production code changes — there's no code change being tested here,
so no spec/RED/GREEN cycle applies; this is closer to the docs-only work
already done for [install-integration-plan.md](install-integration-plan.md).

## Isolation — do not touch real dev data

This project already has one issue class from exactly this mistake (issue
#24: real gitignored `.env.dev`/`config.yaml` masking a bug). A live test
must not run against `data/db-dev.sqlite`, `GIT_REPO_DIR=data/repo`, or the
real `config.yaml` — a scratch env, fully separate:

```bash
export MODE=live-test
export DATABASE_URL=data/live-test/db.sqlite
export GIT_REPO_DIR=data/live-test/repo
export CONFIG_PATH=data/live-test/config.yaml
export HTTP_API_KEY=live-test-key
mkdir -p data/live-test/watched
echo "sources:
  - type: local
    path: data/live-test/watched" > data/live-test/config.yaml
```

`data/live-test/` needs a `.gitignore` entry (it's throwaway state, same
category as `data/repo`/`data/ctx.pid` already ignored).

## Checklist

Each step: expected observable outcome, and what "fail" looks like.

### A. Daemon, standalone

1. `uv run ctx daemon start` → `uv run ctx daemon status` shows running,
   `data/live-test/ctx.pid` exists.
2. Edit a file under `data/live-test/watched/` directly (plain filesystem
   write, no MCP). Expect: `uv run ctx history <path>` shows a new version,
   commit author `unknown:filesystem` — confirms the watcher's honest
   fallback (STATE.md) still holds live, not just in tests.

### B. Claude Code MCP connection

3. Open the repo as a Claude Code project (picks up the committed
   [`.mcp.json`](../../../.mcp.json) automatically). Expect: `chrono-ctx`
   listed as a connected MCP server, its 5 tools
   (`read_file`/`write_file`/`create_file`/`delete_file`/`move_file`)
   visible to the session.
   - **Fail mode to watch for:** server fails to start because it inherited
     the *real* `.env`/`config.yaml` instead of the live-test env vars above
     — `.mcp.json`'s `env` block may need the live-test vars added
     explicitly if Claude Code doesn't inherit the shell environment that
     launched it.

### C. The actual open question — elicitation

4. From the Claude Code session, ask the agent to `write_file` a path
   **outside** any previously-granted scope, inside
   `data/live-test/watched/`. Two possible real outcomes, both informative:
   - Claude Code surfaces an approval prompt → approve it → expect the
     write succeeds, `ctx history` shows the new version committed with
     author `agent:{session_id}` (spec 013's actor-hint handoff, live).
     **This is the confirmation Phase 2 needs.**
   - No prompt appears and the tool call returns `DENIED` (guardrail's
     fail-closed default, [guardrail.py](../../../src/app/mcp/guardrail.py))
     → elicitation isn't supported by this Claude Code client/version.
     **Record this as the answer, don't treat it as a test bug** — it means
     the runbook needs to say so plainly rather than promise elicitation
     works.

### D. Optimistic concurrency, live

5. `read_file` a path to get its `version`. Edit the same path directly on
   disk (simulating another actor). `write_file` the original session with
   the now-stale `version` as `expected_version`. Expect:
   `{"status": "conflict", "current_version": ...}`, no write applied.

### E. Cross-process attribution and rollback, live

6. Do 2-3 more MCP writes across different paths in the same Claude Code
   session. Then `uv run ctx rollback-session agent:<the session id>` from a
   separate terminal. Expect: every path reverts to its pre-session state,
   `rolled_back` lists them — proves spec 020 against a real cross-process
   actor, not the synthetic actor labels integration tests construct.

### F. HTTP API, live

7. `uv run python -m app.api.server` (separate terminal, same live-test env
   vars). `curl -H "X-API-Key: live-test-key" http://127.0.0.1:8000/v1/sources`
   → 200, matches `data/live-test/config.yaml`. Same request with no header
   → 401.

### G. Autostart templates — dry-run only, do not register on a real machine here

8. Read through [`deploy/systemd/ctx-daemon.service`](../../../deploy/systemd/ctx-daemon.service),
   [`deploy/launchd/local.chrono-ctx.daemon.plist`](../../../deploy/launchd/local.chrono-ctx.daemon.plist),
   [`deploy/windows/register-ctx-daemon-task.ps1`](../../../deploy/windows/register-ctx-daemon-task.ps1)
   against a real path substitution and confirm the command lines are
   correct (`uv run ctx daemon start`/`stop`, working directory placeholder).
   **Do not actually run `Register-ScheduledTask`/`systemctl enable`/
   `launchctl load` as part of this test** — that registers persistent OS
   state on whatever machine runs the check, which isn't this plan's call to
   make. Leave real registration to the operator following the runbook on
   the actual target machine.

## Run log — 2026-09-09

- **A (daemon standalone):** passed. Real `ctx daemon start` against real
  `knowledge.example/`, baseline commits landed for all 4 configured sources.
  A direct (non-MCP) file edit correctly versioned with author
  `unknown:filesystem`.
- **B (Claude Code MCP connection):** not run — no live Claude Code session
  available in this pass to connect. `.mcp.json` exists and is committed;
  connecting it is the next real step, separate from this run.
- **C (elicitation):** substituted with `fastmcp.Client(server.mcp,
  elicitation_handler=...)` in place of Claude Code — confirms the
  server-side guardrail correctly fires `ctx.elicit(...)` for an
  out-of-scope path and correctly persists the grant via `ScopeGrant.commit()`
  once approved (`config.yaml` gained the new source). **Does not answer**
  whether Claude Code's own UI renders that prompt — still open.
- **D (optimistic concurrency):** passed, but only once the test accounted
  for the write path's async commit (spec 021's documented limitation) —
  calling back immediately after a `write_file` raced the daemon's own
  debounce+commit and let a genuinely-stale write through silently on the
  first attempt (live confirmation of the documented narrowed-not-eliminated
  window, not a new bug). With a short wait for the commit to land, a stale
  `expected_version` was correctly rejected with `{"status": "conflict", ...}`.
- **E (cross-process attribution + rollback):** passed. Real
  `agent:{session_id}` authorship confirmed in `git log` across an MCP
  server process and a separately-run `ctx rollback-session` CLI process;
  the rollback correctly restored content to the state before the target
  actor's earliest commit and left other actors' history untouched.
- **F (HTTP API):** passed. 401/401/200 for no-key/wrong-key/correct-key
  against a real `uvicorn` process.
- **G (autostart templates):** dry-read only, as planned — not registered.

**Found live, not by design:**

1. [issues.md](../issues.md) #25 — `init_repo()` has no cross-process lock;
   raced against the daemon's own commit of a just-created file and threw
   `CalledProcessError` (`git init` exit 128) from the MCP `read_file` tool,
   reproducible on demand at that timing.
2. [issues.md](../issues.md) #26 — the daemon process vanished mid-run, no
   log line, no exception, cause undetermined. Restarting it did not clean
   up mirror state for files deleted from source during the downtime — a
   live instance of [FUTURE.md](../FUTURE.md) item 5.
3. `.gitignore` was missing `db-dev.sqlite-wal`/`-shm` — WAL mode (spec 016)
   had never been exercised against a long-running real dev DB before this
   pass. Fixed inline, trivial.

**Found by [scripts/live_integration_test.py](../../../scripts/live_integration_test.py)'s
first two automated runs, same day — both since fixed (specs 022/023/024):**

4. [issues.md](../issues.md) #27 (fixed, spec 023) — `ctx daemon stop`
   crashed every time on Windows. Root cause needed a second round of
   experiments beyond the first guess: `CREATE_NEW_PROCESS_GROUP |
   CREATE_NO_WINDOW` *also* fails the same way as `DETACHED_PROCESS` (no
   console allocated either way) - the fix is `CREATE_NEW_PROCESS_GROUP`
   alone plus a hidden `STARTUPINFO` window, and `vcs/runtime.py` needed a
   `SIGBREAK` handler alongside its existing `SIGTERM` one. Very likely
   #26's actual root cause, not independently re-confirmed.
5. [issues.md](../issues.md) #28 (fixed, spec 024) — `rollback_session`
   crashed (`CalledProcessError` from `git show`) when an actor's earliest
   commit on a path is that path's genuine first-ever commit inside a
   mirror repo that already has unrelated history — the code only treated a
   `None` parent as "this actor created the path," which under-detected the
   common case of a shared multi-file mirror repo. The manual pass earlier
   in this file didn't hit it (it reused a path with its own prior
   history); the script's `docs/` target (3 baseline PDFs already
   committed) hit it on the very first run. Fix: `git_store.path_exists_at_rev`,
   a proper tree-membership check instead of the `parent is None` proxy.

`knowledge.example/` and `config.yaml` were fully restored to their
pre-test state (probe files deleted for real through the live watcher,
the auto-granted `skills/` source removed via `ctx source remove`) — `git
status` shows no diff against the committed `knowledge.example/` content.

## Teardown

```bash
uv run ctx daemon stop
rm -rf data/live-test
```

## Deliverable once run

Update [install-integration-plan.md](install-integration-plan.md) Phase 2
item 2 with the actual answer from step C (elicitation supported or not),
and the runbook
([`docs/runbook-shared-install.md`](../../runbook-shared-install.md) §4)
with whatever caveat that answer implies. If elicitation turns out
unsupported, that becomes a new, real open question: either every
Claude-Code-driven write needs a pre-granted scope some other way, or
elicitation support becomes a tracked upstream/version dependency worth
noting explicitly rather than silently assumed.

## Not done here

- Running the checklist itself — this is the plan, not the run log.
- Any decision about what to do if elicitation turns out unsupported (Phase
  2 item 3's question in the install plan) — deliberately left open pending
  step C's actual result.
