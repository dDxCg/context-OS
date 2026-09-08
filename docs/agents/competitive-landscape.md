# Competitive landscape — where chrono-ctx stands, and what to fix first

Research note, not a design doc. Compares chrono-ctx against the closest public
projects (file-watching / versioning MCP servers) and against Claude Code's own
local file-checkpoint system, then turns the gaps into a priority list against
the open items in [issues.md](issues.md).

## Comparables surveyed

| Project | Change detection | Versioning / rollback | Storage | Access control |
|---|---|---|---|---|
| **chrono-ctx** (this repo) | `watchdog` filesystem events, debounced | Multi-version, content-hash blob store; `history`/`rollback`/`diff` **stubbed** (`vcs/services/audit.py`) | SQLite + content-addressed blobs | MCP guardrail: scope check + elicitation approval, fail-closed |
| [cachebro](https://github.com/glommer/cachebro) | Hash-on-read (watcher optional, not load-bearing) | None — diff cache only, no history | Turso (SQLite-compatible) | None |
| [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) | Background watcher, adaptive-interval poll, git-diff based | None — index gets replaced, not versioned | SQLite + WAL, RAM-first | `CBM_ALLOWED_ROOT` static env boundary, per-project mutation locks |
| [knowledge-base-server](https://github.com/willynikes2/knowledge-base-server) | Ingest-triggered | None | SQLite FTS5 | None documented |
| [palinode](https://github.com/Paul-Kyle/palinode) | File watcher, content-hash dedup | Versioning **is** git (delegates entirely) | git + sqlite-vec | Whatever git provides |
| [GitAgent](https://github.com/open-gitagent/gitagent) | N/A — agent lives inside a real git repo | Every change is a git commit (delegates entirely) | git | Whatever git provides |
| **Claude Code** (own checkpointing) | Tool-call interception (Write/Edit/NotebookEdit only — not a filesystem watcher) | Snapshot before every edit, tied to a *conversation checkpoint*; `/rewind` restores code and/or conversation, **atomically across every file touched since that checkpoint** | `~/.claude/.../file-history/`, 30-day auto-expiry | N/A (same agent, same session — no third-party access boundary) |

## What this confirms

**No comparable project does real multi-version storage + rollback for
non-git-backed content.** cachebro and codebase-memory-mcp both explicitly
*replace* state rather than version it. palinode and GitAgent get versioning
"for free" by delegating to git — a different bet than chrono-ctx's (git
requires the source directory to *be* a repo; chrono-ctx targets arbitrary
context sources, e.g. exported docs, non-repo folders, 3rd-party pulls, where
that assumption doesn't hold). **That makes `history`/`rollback`/`diff` the
whole differentiator, and it's currently a stub.**

**No comparable MCP server has interactive, scope-based access control.**
codebase-memory-mcp's `CBM_ALLOWED_ROOT` is static and coarse (one root at
startup). chrono-ctx's elicitation-based guardrail — request scope, human
approves, `config.yaml` grows exactly one entry, fail-closed on refusal — is
more sophisticated than anything found. Worth finishing and documenting as a
selling point once the MCP tools are actually wired to it (see gap 3 below).

**Claude Code's own design validates one specific gap chrono-ctx doesn't yet
have an answer for: multi-file atomic rollback.** A `/rewind` restores every
file touched since a checkpoint, in one action. chrono-ctx's planned
`rollback` is per-file only (`ctx rollback <file> -v N` per the README). An
agent session realistically touches several context files per run — there is
currently no way to undo "everything session/config-diff X did" as one unit.

## Gaps, mapped to existing tracking

1. **Multi-file / session-scoped rollback — no tracking issue yet, biggest
   product gap.** Needs a run/session id threaded through ingestion so
   `rollback` can target a batch, not just one file. Should land alongside
   the `audit.py` stub work, not after it — retrofitting a session id onto
   an already-shipped per-file schema is more painful later.
2. **[issues.md](issues.md) #17** — watcher-created files never get a blob
   written, only the DB row. Directly breaks rollback correctness for any
   file versioned outside the startup scan — i.e. most of them, including
   every MCP-guardrail-approved file. This is the differentiator from
   §"What this confirms" shipping broken if left open.
3. **[issues.md](issues.md) #18** — no same-hash guard in `modified_handle`;
   every no-op edit doubles the version count. Makes `history` noisy and
   `diff` report nothing between adjacent versions — undermines the same
   differentiator from the UX side.
4. **MCP tools not wired to versioning.** `read/write/create/delete/move_file`
   are stubs (README, "Trạng thái hiện tại"). The guardrail is real but has
   nothing behind it yet — the access-control advantage over
   codebase-memory-mcp is currently unrealized.
5. **No retention/GC.** Claude Code auto-expires checkpoints at 30 days.
   chrono-ctx has no pruning at all, and #18 actively multiplies the
   unpruned blob count. Long-lived-knowledge-base use case argues against a
   fixed TTL like Claude Code's, but an explicit prune/GC path (age- or
   count-based, user-triggered) is still needed before storage growth
   becomes a real problem.
6. **Audit reads don't (yet, design undecided) re-check scope.** Claude
   Code's rewind is safe by construction — same agent, same session, no
   third party. chrono-ctx's guardrail enforces scope at MCP call time for
   live read/write, but nothing in `audit.py`'s stub says `history`/
   `rollback`/`diff` re-run `is_path_in_scope` before returning old content.
   An agent whose scope was revoked could otherwise still read a file's
   history through the audit path. Decide this now, before the stub fills
   in — should fail-closed the same as live access.
7. **[issues.md](issues.md) #20** (WAL + busy timeout) is table stakes, not
   novel — codebase-memory-mcp already ships WAL plus per-project mutation
   locks for CLI/daemon contention. Confirms #20 is correctly scoped, not
   over-engineering.
8. **No hash-verify fallback if a watcher event is dropped.** cachebro
   sidesteps this by hashing on every read instead of trusting the watcher;
   codebase-memory-mcp polls at adaptive intervals as backup. chrono-ctx
   only reconciles via `sync_source_status` at restart — a dropped OS event
   (adjacent to but distinct from [issues.md](issues.md) #21/#22) leaves the
   DB wrong with no self-heal until the process restarts. Not yet a filed
   issue; worth one if a periodic/on-read reconcile isn't planned elsewhere.

## What's already a genuine strength (do not change)

- **Content-addressed blob storage.** Automatic dedup across files/versions
  with identical content, once #18's guard is in. cachebro and
  codebase-memory-mcp don't get this for free the way chrono-ctx's design
  already does.
- **Filesystem-watcher capture surface is broader than tool-call
  interception.** Claude Code only sees its own Write/Edit/NotebookEdit
  calls; chrono-ctx also catches out-of-band edits — a human hand-editing a
  file, or an agent using a shell command that bypasses MCP tools entirely.
  That's real coverage Claude Code's model can't have by construction —
  justifies the ongoing investment in watcher/bus robustness
  ([pubsub-plan.md](pubsub-plan.md), [dir-events-plan.md](dir-events-plan.md))
  rather than narrowing scope to MCP-tool-only tracking.

## Priority order

1. **#17** (blob never written on watcher path) — correctness prerequisite
   for everything below.
2. **#18** (duplicate versions) — same reason, plus directly bloats whatever
   GC design comes later.
3. **Session/batch id for rollback** — design it into the `audit.py` build,
   not bolted on after.
4. **Wire MCP tools to versioning** — realizes the guardrail's advantage.
5. **Scope-check on audit reads** — decide and implement alongside 3–4, not
   after the stub ships without it.
6. **#20** (WAL + busy timeout) — needed once CLI and daemon coexist.
7. **Dir events (#21/#22)** — already planned, see
   [dir-events-plan.md](dir-events-plan.md).
8. **Retention/GC** and **hash-verify fallback** — file as new issues once
   the above land; both are storage-growth / durability concerns, not
   blockers to shipping the core differentiator.
