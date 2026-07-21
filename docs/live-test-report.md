# Live test report — MCP ↔ VCS runtime, 2026-07-20

First end-to-end exercise of the **two-process** path: the real `VCSRuntime`
(`python -m vcs.runtime`) running as a subprocess, driven by the real MCP
server (`app.mcp.server`) through an in-process `fastmcp.Client` with an
elicitation handler. The two share nothing but `config.yaml` on disk.

This closes the gap [mcp-test-plan.md](mcp-test-plan.md) lists as
"End-to-end watcher pickup of MCP-driven changes". Every unit test in the
suite mocks the watcher, so none of what follows is reachable from them.

**Result: 16/16 core scenarios pass, 4/5 edge probes clean. Five issues
found, all in the MCP tool layer or configuration — none in the watcher,
queue, or versioning paths.**

Harness scripts are not checked in; they lived in the session scratchpad
(`live_test.py`, `edge_test.py`, `encoding_test.py`, `deadlock_probe.py`,
`cwd_test.py`).

> **Status: Issues 1–4 fixed and verified.** The dedicated encoding harness
> went from 0/3 to 3/3, `live_test.py` held at 16/16 with no regression in
> the watcher path, and a new cwd-divergence harness confirms Issue 4.
> The carried-over worker-durability gap is fixed too. The suite grew from
> 88 to 110 tests, including a new `tests/unit/utils/test_helper.py` — those
> helpers previously had zero coverage, which is why the encoding bug
> shipped. Issue 5 needs no code change; see its entry. Per-issue notes are
> inline below.

---

## What passed

| # | Scenario | Result |
|---|---|---|
| A | In-scope read succeeds without prompting | pass |
| B | Out-of-scope read, declined → denied, config untouched | pass |
| C | Three rapid successive approvals all ingested at `status=1` | pass |
| D | Unapproved sibling in a watched directory is never versioned | pass |
| E | Second read of an approved path skips elicitation | pass |
| F | MCP `write_file` picked up by the watcher as a new version | pass |
| G | MCP `delete_file` flips `locations.status` to 0 | pass |
| H | `terminate()` exits promptly; no tracebacks, no dead threads | pass |
| 1 | 8 concurrent approvals all persist to `config.yaml` | pass |
| 2 | `../` traversal out of a source is denied | pass |
| 3 | Approving the DB file causes no runaway self-versioning | pass |

Specifically confirming the watch-target decoupling work: approving files
in a new directory grew the watch set by exactly one entry (the parent
directory), and D confirms the router's scope filter keeps unapproved
siblings under that broader watch from being versioned.

---

## Issue 1 — MCP file I/O silently corrupts non-ASCII text (critical)

**Files:** `src/utils/helper.py:25-33`, used by all of `src/app/mcp/server.py`

`read_file` and `save_to_file` call `open()` in text mode with **no
`encoding=`**, so they use the platform default — `cp1252` on this machine,
and locale-dependent everywhere else.

Reading a normal UTF-8 file through the MCP `read_file` tool returns
`{"status": "ok", ...}` with mojibake content:

```
on disk:   # Café — naïve 日本語 🎉
returned:  # CafÃ© â€” naÃ¯ve æ—¥æœ¬èªž ðŸŽ‰
```

This is the worst failure mode in this report: **no error is raised and the
status is `ok`**, so an agent has no way to detect it. If the agent then
writes that string back through `write_file`, the corruption is persisted
and the watcher faithfully records the mangled version.

It is also inconsistent with the rest of the codebase, which already handles
encoding correctly: `_dump_config`/`recover_config` in
`vcs/services/configure.py` pass `encoding="utf-8"` explicitly, and the
versioning path reads bytes (`ContextEntry.from_path` → `read_bytes()`), so
blobs and hashes are unaffected. Only the MCP surface is broken.

**FIXED.** `helper.read_file`/`save_to_file` now take an `encoding` argument
(default UTF-8) applied only when `"b" not in mode`, so the bytes-based
versioning path is provably untouched. New `read_text_file()` decodes
strictly first and falls back to `errors="replace"` only on failure,
returning `(content, lossy)` so the caller can tell — see Issue 2 for how
the MCP layer surfaces that.

`newline=""` was also applied to both directions, which fixed a second
latent corruption found while writing the tests: text mode translates `\n`
to `\r\n` on write and back on read, so an agent read/write round-trip on an
LF file silently converted it to CRLF — mutating content and producing
spurious versions.

## Issue 2 — MCP tools raise unhandled exceptions on undecodable files (high)

**File:** `src/app/mcp/server.py` (every tool body)

Each tool catches only `OSError`. `UnicodeDecodeError` and
`UnicodeEncodeError` derive from `ValueError`, so they escape the handler
and surface as a FastMCP `ToolError` instead of the documented
`{"status": "error", "reason": ...}` contract that every other failure uses.

Reproduced twice:
- `read_file` on a SQLite DB → `UnicodeDecodeError: 'charmap' codec can't decode byte 0x8d`
- `write_file` with `"Café 日本語 🎉"` → `UnicodeEncodeError: 'charmap' codec can't encode characters in position 5-7`

Any binary file (image, PDF, `.sqlite`, compiled artifact) inside an
approved source will do this. Fixing Issue 1 removes the *common* case but
not this one — a genuinely binary file still can't be decoded as UTF-8.

A third escape turned up during the fix that this report originally missed:
`shutil.Error`, raised by `move_file`, is not an `OSError` subclass either.

**FIXED.** All five tools now catch a shared
`IO_ERRORS = (OSError, UnicodeError, shutil.Error)`. Deliberately not bare
`Exception`, so genuine programming errors still surface loudly.

`read_file` additionally returns a `lossy` boolean. `lossy: true` means bytes
could not be decoded as UTF-8 and were replaced — the file is not text.
Reads therefore never fail (the chosen policy), but an agent can still tell
it is holding degraded content before writing it back, which is what made
`errors="replace"` safe to adopt here.

## Issue 3 — Scope is granted even when the operation fails (medium)

**File:** `src/app/mcp/guardrail.py:23-25`

`ensure_scope` calls `add_sources([path])` on approval, *before* the tool
attempts its work. When the subsequent operation fails, the grant is not
rolled back.

Observed directly in edge probe 3: `read_file` on the DB file raised, the
tool returned an error — and `config.yaml` had still grown from 9 to 10
sources. The path is now permanently in scope, contributes a watch target,
and gets ingested and versioned, despite the read the user approved having
never succeeded.

Low blast radius (the user *did* approve that exact path), but the config
accumulates entries for operations that never happened, and combined with
Issue 2 an agent can widen scope through calls that only ever error.

**FIXED.** `ensure_scope` now returns a `ScopeGrant` instead of a bool. The
scope *check* still happens before the operation — that is the security
boundary and did not move. Only the *persistence* moved: tools call
`grant.commit()` after the operation succeeds, and `commit()` is a no-op for
paths that were already in scope. `move_file` holds two grants and commits
both only once the move lands.

Note the original live probe no longer demonstrates this, because with
Issue 1 fixed the DB read now succeeds (lossy) and the grant legitimately
commits. The regression is covered by
`test_failed_operation_does_not_persist_the_scope_grant` instead.

## Issue 4 — `CONFIG_PATH` is relative, and the two processes need identical cwd (medium)

**Files:** `.env` (`CONFIG_PATH=config.yaml`), `Dockerfile:14`

`config.yaml` is the *only* coupling between the MCP server and the VCS
runtime. `get_config_path()` returns the raw env value, and `.env` ships it
relative — so both processes agree only if they have the same working
directory.

The MCP server runs over **stdio**, meaning its cwd is chosen by whatever
client spawns it (Claude Desktop, the FastMCP Inspector, an IDE). If that
differs from where `vcs.runtime` was started, the guardrail writes approvals
into one file while the watcher reads another. The failure is silent: MCP
calls succeed, and nothing is ever versioned.

`vcs/shared/config.py` has the same property — `SNAPSHOT_DIR`,
`BLOB_DIR`, and `CONFIG_SNAPSHOT_FILE` are all module-level relative paths
with no env override.

**FIXED.** `helper.anchored()` resolves relative configured paths against
`PROJECT_ROOT` (derived from the package location) rather than cwd, so both
processes agree no matter where they were launched. Applied to
`CONFIG_PATH`, `DATABASE_URL`, `SCHEMA_PATH`, the `load_dotenv()` calls
themselves, and `SNAPSHOT_DIR`/`BLOB_DIR`/`CONFIG_SNAPSHOT_FILE` in
`vcs/shared/config.py`. Absolute values pass through untouched, so test
fixtures and the Docker image are unaffected.

Absolutising against cwd would *not* have fixed this — it only makes each
process internally consistent while still letting the two disagree.

Two things fixed alongside: `get_config_path()` now defaults to
`"config.yaml"` instead of returning `None` (which detonated later inside
`path_normalize` during `Config*Event` construction), and `SNAPSHOT_DIR`
gained an env override so a second instance can be pointed at its own data
directory — previously impossible, and needed to run the live harnesses
without polluting the repo.

Verified by `cwd_test.py`: the runtime launched from an unrelated temp
directory picks up an approval written by an MCP process running in the repo
root, with `CONFIG_PATH` left relative.

## Issue 5 — Verbose logging can freeze the runtime if stdout is not drained (low / environmental)

**Files:** `src/utils/logger.py`, `@log_enabled` decorators throughout

In `MODE=dev` the runtime logs at DEBUG, and `@log_enabled` wraps nearly
every function in the event pipeline. If that log stream is a pipe that
nobody reads, the ~4 KB OS pipe buffer fills within about 30 log lines and
the child blocks on `write()` — which freezes **every** thread that logs,
i.e. the whole pipeline.

*Correction to the original draft of this report, which said stdout:* the
stream is **stderr**. `setup_logger` calls `logging.basicConfig` with no
`stream=`, and that default handler writes to stderr. The hazard is
unchanged — the harness merged stderr into the same undrained pipe via
`stderr=subprocess.STDOUT` — but the detail was wrong.

For the stdio MCP server, stderr is the *correct* stream: stdout carries
JSON-RPC, so anyone adding `stream=sys.stdout` to `setup_logger` would
corrupt the protocol. Worth a comment there before someone tries it.

Recording this because of how it presents: total, silent, permanent stall
with no traceback and no dead thread. It cost real diagnosis time in this
session — the first live run failed 3 of 16 checks and looked exactly like a
watcher deadlock. A `faulthandler` thread dump proved every thread was
healthy and idle, and the true cause was the test harness holding an
undrained `subprocess.PIPE`. **No product bug existed.** After draining
stdout, the same 3 checks passed.

Docker is unaffected (the logging driver drains it). Any supervisor or
wrapper that pipes without reading is not.

**No fix applied — no product bug exists here.** Consider defaulting to INFO
outside dev, or logging to a rotating file handler.

---

## Worker durability (carried over from earlier review) — FIXED

`ConsumerWorker.run()` had no `try/except` around `self.consumer.handle(event)`,
so one bad event killed that consumer for the remaining process lifetime —
`Thread.run` swallows the traceback into `threading.excepthook` and nothing
recovers.

Not triggered during the live run, but Issues 1–2 showed unhandled
`UnicodeError`s are reachable, and the config consumer now performs inline DB
writes where a single unreadable file would take the thread down.

`handle()` is now wrapped: the failure is logged and the loop continues.
`queue.close()` moved into a `finally` so an escape cannot leave the queue
open, and the pre-loop `from_db_url()`/`_configure_consumer()` are guarded
too — a DB-connect failure previously killed the worker *before* its loop
began, leaving a process that looks healthy while consuming nothing.
Covered by `tests/unit/vcs/workers/test_consumer_worker.py`.

---

## Verification performed

| Harness | Before | After |
|---|---|---|
| `encoding_test.py` | 0/3 | **3/3** |
| `live_test.py` | 16/16 | 16/16 (no regression) |
| `edge_test.py` | 4/5 | 4/5 (remaining check now asserts the wrong property — see Issue 4) |
| `cwd_test.py` | n/a | **pass** (new; runtime and MCP in different cwds) |
| `pytest` | 88 | **110** |

## Follow-ups not taken

- **`edge_test.py` probe 4** checks whether `.env` holds an absolute
  `CONFIG_PATH`. That was the right check before anchoring; now a relative
  value is safe and the probe should be rewritten to assert cross-cwd
  agreement instead, as `cwd_test.py` does.
- **MCP tools are `async def` but perform synchronous blocking I/O**, so they
  block the event loop. Harmless at current usage; worth revisiting if
  concurrent tool calls or large files become common.
- **`add_sources` is an unlocked read-modify-write.** Eight concurrent
  approvals landed cleanly (probe 1), but that ran on a single asyncio loop.
  Two MCP *processes* against one `config.yaml` could still interleave.
- **`DEBUG` is defined in `.env.dev`/`.env.prod` but read nowhere** in `src/`.
  Dead config.
