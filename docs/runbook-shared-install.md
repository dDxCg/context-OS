# Runbook — helpdesk-installed shared setup

For the technical team / helpdesk setting up chrono-ctx once on a shared
machine or server, so non-technical users need zero install steps of their
own. See [`docs/agents/draft/install-integration-plan.md`](agents/draft/install-integration-plan.md)
for the reasoning behind this shape; this doc is the step-by-step.

Scope: one machine, one daemon, one or more watched folders. Multi-machine
distribution is a separate, unbuilt effort — see
[FUTURE.md](agents/FUTURE.md) item 6.

## 1. Install

```bash
git clone https://github.com/dDxCg/chrono-ctx.git && cd chrono-ctx
uv sync
cp config.example.yaml config.yaml   # point `path:` entries at the real shared folder(s)
```

Requires `git` and `uv` on PATH for whichever account runs the daemon.

## 2. Verify it runs

```bash
uv run ctx daemon start
uv run ctx daemon status
```

Non-technical users can now edit files in the watched folder(s) through
whatever tool they already use — nothing else to install on their end. Every
edit is versioned transparently, attributed `unknown:filesystem` unless it
came through an MCP tool (see step 4).

## 3. Autostart on boot

`ctx daemon start` does not survive a reboot by itself — something has to
call it again after the machine restarts. Templates under
[`deploy/`](../deploy):

- **Windows**: run [`deploy/windows/register-ctx-daemon-task.ps1`](../deploy/windows/register-ctx-daemon-task.ps1)
  from the repo root — registers a Scheduled Task that runs `ctx daemon
  start` at log on.
- **Linux**: copy [`deploy/systemd/ctx-daemon.service`](../deploy/systemd/ctx-daemon.service)
  to `~/.config/systemd/user/`, edit `WorkingDirectory` to the real repo
  path, then `systemctl --user daemon-reload && systemctl --user enable --now
  ctx-daemon`.
- **macOS**: copy [`deploy/launchd/local.chrono-ctx.daemon.plist`](../deploy/launchd/local.chrono-ctx.daemon.plist)
  to `~/Library/LaunchAgents/`, edit `WorkingDirectory`, then `launchctl load`
  it.

## 4. Register an agent (Claude Code)

The repo root ships a committed [`.mcp.json`](../.mcp.json) pointing Claude
Code at `uv run python -m app.mcp.server` (stdio). Opening the repo as a
Claude Code project picks it up automatically — no separate registration
step for the operator.

**Open item, not yet confirmed:** every MCP read/write goes through a
scope-approval guardrail (elicitation, fail-closed on no support —
[`app/mcp/guardrail.py`](../src/app/mcp/guardrail.py)). Whether Claude Code's
MCP client renders that elicitation prompt hasn't been verified against a
live session yet. Fail-closed means the safe direction if it doesn't (writes
get denied, not silently allowed) — but confirm this before promising a
non-technical user that "just use Claude Code" works end to end.

## 5. Audit stays with the technical team

Non-technical users never run these — this is the operator's/technical
team's job:

```bash
uv run ctx source list
uv run ctx history <source>
uv run ctx diff <source> <rev>
uv run ctx rollback <source> <rev>
uv run ctx rollback-session <actor_label>
```
