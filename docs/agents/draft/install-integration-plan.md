# Draft — simple install & integration plan (tech / non-tech / agent)

Status: **draft, not started.** Scopes the "who installs what, who's
responsible for what" question for the three audiences this project already
targets ([README.md](../../../README.md)'s Target users section), for the
most basic real scenario: **Claude Code as the MCP client, a shared/cowork
folder as the source.** Not a spec yet — a spec gets written per piece once
someone is about to implement it (`AGENTS.md`'s rule).

## Ground rule this plan is built on

The three audiences do **not** get symmetric responsibility:

| Audience | Installs | Interacts via | Owns audit? |
| --- | --- | --- | --- |
| Non-technical user | Nothing (pre-provisioned) or a single guided step | Their normal file editor, on the watched folder | **No** |
| Technical user / operator | `chrono-ctx` itself (`uv sync`, daemon, config) | CLI (`ctx ...`), maybe HTTP API | **Yes** |
| Agent (Claude Code, etc.) | Nothing — the human operator registers it | MCP (stdio) | No — same as non-tech, just a different actor identity |

This matches what was asked: non-technical users only ever *use* the system
(edit files where they already work); the technical team is the one who
installs the daemon, runs `ctx history`/`ctx diff`/`ctx rollback*`, and is
accountable for what happened. This also means **FUTURE.md item 3
(non-technical approval channel) is explicitly out of scope here** — that
item is about a non-tech user *approving* an agent's scope request, a
different problem from a non-tech user *editing files*. Nothing in this plan
needs it.

## Why this already mostly works today, structurally

The watcher captures every filesystem change independent of who made it —
this project's actor-attribution model was deliberately built honest, not
narrowed (see [STATE.md](../STATE.md) "Actor attribution"). A non-technical
user saving a file in a watched folder through Explorer/Finder/Word produces
a real, versioned, git-mirrored commit today, attributed
`unknown:filesystem`, with zero new code. The gap isn't the versioning
engine — it's getting the daemon running on their machine (or a shared one)
and getting an agent (Claude Code) pointed at the right MCP server. That's
what this plan actually covers.

## Phase 1 — Helpdesk-installed shared setup (no new code, do this first) — done

Runbook written: [`docs/runbook-shared-install.md`](../../runbook-shared-install.md).
Autostart templates: [`deploy/systemd/ctx-daemon.service`](../../../deploy/systemd/ctx-daemon.service),
[`deploy/launchd/local.chrono-ctx.daemon.plist`](../../../deploy/launchd/local.chrono-ctx.daemon.plist),
[`deploy/windows/register-ctx-daemon-task.ps1`](../../../deploy/windows/register-ctx-daemon-task.ps1).

Recommended starting point for "cơ bản nhất" (most basic case): **one
machine (or one shared server), one daemon, one or more watched folders**,
with:

- Technical team / helpdesk clones the repo, `uv sync`, `cp
  config.example.yaml config.yaml` pointed at the shared source folder(s),
  `ctx daemon start`. Everything in README's Quick start, already works.
- Non-technical users get **no install step at all** — they already have
  access to the watched folder (network share, synced drive, whatever they
  use today) and keep editing exactly as before. The daemon versions it
  transparently.
- Claude Code (or another agent) is registered against the **same** machine's
  MCP server by whoever operates it — see Phase 2, this is the actual gap.
- Audit stays with the technical team: `ctx history <source>`, `ctx diff`,
  `ctx rollback`/`ctx rollback-session` when something needs undoing.

This needs a short **runbook doc** (not code) — README's Quick start is
written for a developer setting up their own dev environment, not a
step-by-step a helpdesk person can follow blind. Concretely still missing:
autostart-on-boot instructions per OS (`ctx daemon start` today only
survives until next reboot — nothing runs it again), since a helpdesk-managed
shared machine needs the daemon to survive a restart without anyone present:

- **Windows**: Task Scheduler trigger "at log on"/"at startup" running `ctx
  daemon start`, working directory pinned to the repo.
- **Linux**: systemd user service (`WantedBy=default.target`) or `@reboot`
  cron entry running the same command.
- **macOS**: launchd user agent plist, `RunAtLoad`.

None of this is built — it's three short doc sections (or, later, three
template files under e.g. `deploy/`), not new application code. `ctx daemon`
itself already does the hard part (spec 019); this is only "make the OS call
`ctx daemon start` once, after boot."

## Phase 2 — Claude Code integration (the real gap) — partly done

Checked: this repo has [`fastmcp.json`](../../../fastmcp.json) (FastMCP's own
run-config, consumed by `fastmcp run`) but had **no `.mcp.json`** — the file
Claude Code actually reads to register a project-scoped MCP server. These are
two different formats for two different tools; having one does not give you
the other.

1. Done — committed [`.mcp.json`](../../../.mcp.json) at the repo root,
   `command`/`args` pointing at `uv run python -m app.mcp.server` (stdio). No
   machine-specific `cwd` baked in: Claude Code resolves a project-scoped
   `.mcp.json`'s working directory to the project root itself, which is
   exactly where `DATABASE_URL`/`CONFIG_PATH`/`GIT_REPO_DIR` already anchor
   relative to (see [STATE.md](../STATE.md) anchored-path notes and spec
   015) — opening the repo as a Claude Code project is the only registration
   step left for the operator.
2. **Still open, needs verification before this is promised to work** — plan
   and 2026-09-09 partial run: [live-integration-test-plan.md](live-integration-test-plan.md).
   That run confirmed the server-side guardrail mechanism itself works
   (`ctx.elicit(...)` fires correctly, approval persists into `config.yaml`)
   using an in-process stand-in client, not Claude Code — the actual
   question, does Claude Code's MCP client **render** that elicitation
   prompt (the scope-approval guardrail every read/write goes through,
   fail-closed on no-elicitation-support — see
   [app/mcp/guardrail.py](../../../src/app/mcp/guardrail.py)), is still
   unanswered. If not, every Claude Code write is silently denied, not
   silently approved (fail-closed is the safe direction), but that makes the
   tool unusable from Claude Code until confirmed. Needs an actual live
   Claude Code session (step B of the live-test plan, not yet run) before
   this is promised to work.
3. Whether the technical operator pre-approves scope once (if Claude Code
   supports remembering elicitation responses) or a non-technical user
   sitting at a Claude Code session would actually see and need to answer an
   approval prompt themselves — the latter would be a real, if narrow,
   instance of the non-tech-approval gap FUTURE.md item 3 describes, worth
   re-flagging there if it turns out to matter for this basic case.

## Phase 3 — "Cowork" scope, stated explicitly (avoid overpromising)

"Cowork" here means: **multiple actors (human + agent, or several of either)
editing the same watched folder concurrently, on one daemon.** That's already
handled — single-writer lock (spec 012) plus MCP `expected_version`
optimistic-concurrency (spec 021) so a lost update surfaces as a conflict
response instead of silently vanishing.

What "cowork" does **not** mean here: multiple *machines*/branch offices each
with their own daemon, centrally reconciled. That's FUTURE.md item 6
(RabbitMQ central-writer distribution) — explicitly parked pending a real
driver, and this plan doesn't change that. If the actual near-term need turns
out to be multi-machine (not just multi-actor on one shared folder), that's a
different, bigger plan, not this one.

## Phase 4 — Self-serve installer for non-tech users (later, only if needed)

Not started, not clearly needed yet. Only worth building if Phase 1's
"helpdesk pre-installs, non-tech user never touches setup" model turns out
insufficient for the real rollout (e.g. no helpdesk function exists, or
users need chrono-ctx on machines helpdesk doesn't manage). Shape, if it
becomes needed: a single OS-native installer script wrapping Phase 1's
runbook (clone/sync, default `config.yaml`, autostart registration) so a
non-technical user runs one command/installer instead of following the
runbook — still not something they'd configure, just execute. Packaging as a
single binary (no `uv`/Python on PATH assumption) is blocked on the same
constraint [STATE.md](../STATE.md) already flags: the storage backend shells
out to the real `git` binary, so a single self-contained executable isn't
free even then.

## Not done here

- Verifying Claude Code's elicitation support (Phase 2, item 2) — a real
  check against a live Claude Code session, not something this doc (or
  writing more files) can settle by reasoning.
- Any self-serve installer (Phase 4) — explicitly deferred pending Phase 1
  proving insufficient.
