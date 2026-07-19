# MCP server test plan

What's tested for `src/app/mcp/server.py`, how, and what's known to be
untested — for a reviewer or contributor judging coverage without having
to reverse-engineer it from the test files.

## Scope

Under test here: the five MCP tools (`read_file`, `write_file`,
`create_file`, `delete_file`, `move_file`) and the guardrail in front of
them (`src/app/mcp/guardrail.py::ensure_scope`, backed by
`vcs.services.configure.is_path_in_scope`).

Not under test here (covered elsewhere, or not built yet):

- The watcher/versioning pipeline itself — see `tests/unit/vcs/`.
- The RabbitMQ migration — not implemented, see
  [rabbitmq-migration.md](rabbitmq-migration.md).
- Whether the watcher picks up MCP-driven filesystem changes — blocked on
  [issues.md](issues.md) issue #1, see "Known gaps" below.

## Test levels

- **Unit** — `is_path_in_scope()` boundary cases:
  `tests/unit/vcs/services/test_configure.py` (exact path match, nested
  file under a source, path outside every source, non-`local` source
  types ignored).
- **Integration (in-process MCP client)** — `tests/unit/app/mcp/
  test_guardrail.py`, 10 tests. Uses `fastmcp.Client(server.mcp,
  elicitation_handler=...)` against the real `FastMCP` app — real
  filesystem assertions (files actually written/moved/deleted on disk),
  real `ElicitResult` accept/decline simulation via a custom elicitation
  handler. This is the bulk of current coverage.
- **Manual/live smoke** — a script combining a real `VCSRuntime` (watcher
  running) with a real `fastmcp.Client` call, e.g. the pattern kept at
  `scratchpad/smoke_mcp_plus_watcher.py` (not checked in). Used to verify
  end-to-end wiring beyond what the in-process client alone proves, and
  currently to demonstrate issue #1 (the watcher doesn't yet pick up
  MCP-driven changes). Reuse this pattern for similar checks; it isn't an
  automated suite.

## Coverage matrix

Rows are tools, columns are scenarios. ✅ = covered by an existing test in
`tests/unit/app/mcp/test_guardrail.py`, ❌ = not covered.

| Tool          | in-scope success | out-of-scope + approve | out-of-scope + decline | missing-file / OSError | src in / dst out | src out / dst in |
|---------------|:---:|:---:|:---:|:---:|:---:|:---:|
| `read_file`   | ✅ `test_read_file_returns_content_for_in_scope_path` | ✅ `test_out_of_scope_approval_persists_to_config_and_succeeds` | ❌ | ✅ `test_read_file_missing_returns_error` | — | — |
| `write_file`  | ✅ `test_write_file_overwrites_existing_content_on_disk` | ❌ | ✅ `test_out_of_scope_decline_blocks_and_leaves_config_untouched` | ❌ | — | — |
| `create_file` | ✅ `test_create_file_writes_content_and_makes_parent_dirs` | ❌ | ❌ | — (creates, so no missing-file case) | — | — |
| `delete_file` | ✅ `test_delete_file_removes_existing_file` | ❌ | ❌ | ✅ `test_delete_file_missing_returns_error` | — | — |
| `move_file`   | ✅ `test_move_file_succeeds_when_both_src_and_dst_in_scope` | ❌ | ❌ | ❌ | ❌ (not exercised) | ✅ `test_move_file_checks_both_src_and_dst` |

The ❌ cells above are mostly redundant with the ✅ cells on other rows —
`ensure_scope()` is the same function call in all five tools, so the
approve/decline/missing-file behavior it's already proven for
`read_file`/`write_file`/`delete_file` applies uniformly to
`create_file`/`move_file` too. They're listed as gaps for completeness,
not flagged as urgent.

## Known gaps

Explicitly not covered today, and why that's an acknowledged gap rather
than an oversight:

- **Elicitation `cancel`** and a client that doesn't support elicitation
  at all — only `accept`/`decline` are exercised
  (`_approve`/`_decline` handlers in `test_guardrail.py`).
  `ensure_scope()` is documented to fail closed on both per
  [rabbitmq-migration.md](rabbitmq-migration.md), but no test asserts it.
- **Concurrent/overlapping calls** racing on `config.yaml` writes from
  `ensure_scope`'s approval path — no locking exists in
  `vcs.services.configure`, and it isn't tested.
- **Path traversal / symlink edge cases** against `is_path_in_scope`'s
  `Path.is_relative_to()` check — not exercised beyond plain nested-path
  cases.
- **End-to-end watcher pickup of MCP-driven changes** — blocked on
  [issues.md](issues.md) issue #1 (shared-queue race); not something a
  test can assert correctly until that's fixed.
- **`read_file`'s `version` field is always `None`** — by design, not a
  gap: MCP I/O is intentionally decoupled from `vcs.services.versioning`
  (the independent watcher is the source of truth for tracking), so
  there's no version number to report at call time.

## Manual/interactive testing (MCP Inspector)

For exercising the server like an API, no code needed:

1. Run `uv run fastmcp dev inspector` from the repo root — starts the
   server and opens the MCP Inspector web UI, the closest equivalent to
   Postman for MCP: it lists all five tools, gives a form for each tool's
   parameters, and shows the raw request/response. No path argument is
   needed: the repo-root `fastmcp.json` points Inspector at
   `src/app/mcp/server.py` (`mcp` entrypoint) and tells it to run inside
   the project's own `.venv` (`environment.project: "."`, i.e. `uv run
   --project .` under the hood). The Inspector UI URL (with its session
   token) is printed to the terminal once it's up — open that URL in a
   browser.
2. Prerequisite: a `config.yaml` with at least one `local` source needs
   to exist (or `CONFIG_PATH` pointed at one) before starting the
   Inspector — same requirement as running the real app.
3. Suggested walkthrough:
   - `read_file`/`write_file` on a path inside a configured source →
     expect `{"status": "ok", ...}`.
   - The same on a path outside every source → expect an elicitation
     prompt in the Inspector; approve it once, decline it once, and
     check whether `config.yaml` gained the approved path afterward.
   - `read_file`/`delete_file` on a path that doesn't exist → expect
     `{"status": "error", "reason": ...}`.

This is manual/exploratory — use it to sanity-check behavior
interactively or demo the guardrail, not as a substitute for the
automated suites above.

## How to run the automated suite

```bash
pytest tests/unit/app/mcp/ tests/unit/vcs/services/test_configure.py -v
pytest --cov=src --cov-report=term-missing
```
