# Chrono Context - Version control for an AI agent's context sources

[![CI](https://github.com/dDxCg/chrono-ctx/actions/workflows/ci.yaml/badge.svg)](https://github.com/dDxCg/chrono-ctx/actions/workflows/ci.yaml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

🇬🇧 English · [🇻🇳 Tiếng Việt](README.vi.md)

## Problem

- Context fed to an AI agent (docs, prompts, workflows) changes constantly
  and nothing tracks its version — the agent can read a stale or corrupted
  copy with no way to roll back or tell who changed what and when.
- Turning the source directory itself into a git repo doesn't fit: sources
  live scattered across many places, not everyone wants a `.git` mixed into
  their documents, and an agent or a non-technical user can't drive git
  directly.
- There's no standard lookup surface — for an agent (MCP) or for another
  system (HTTP) — to ask "what changed in this file, when, by whom" without
  importing the codebase directly.

## Target users

- **AI agent** (via MCP): reads/writes context sources, needs to know the
  current version, needs a scope guardrail (no read/write outside approved
  scope).
- **Developer/operator** (via CLI + daemon): declares which sources to
  track, runs the watch daemon in the background.
- **Another system / future approval UI** (via HTTP API): read-only history/
  diff lookups, no Python import required.

## Solution

```
Filesystem  --watch-->  VCS Runtime daemon  --commit-->  Git mirror repos (data/repo/)
                              |                                    ^
                              v                                    |
                       SQLite (identity: contexts/locations)        |
                                                                     |
AI agent  --MCP (stdio)-->  MCP server  --plain fs I/O-->  Filesystem (watcher picks it up)
HTTP client  --GET /v1/*-->  HTTP API  --read-only-->  audit.py --> git mirror repos
```

Detailed architecture (container/sequence diagrams, data model, concurrency,
actor attribution, design decisions) → [`ARCHITECTURE.md`](ARCHITECTURE.md).

| Feature | What it does | Status |
| --- | --- | --- |
| Watch + versioning engine | Watches local sources, a similarity gate decides whether to cut a new version, commits into a git mirror per watch target | Done |
| Git mirror backend | One git repo per watch target, not the source directory itself — least-privilege by scope | Done |
| `audit.py` (`get_sources`/`get_version_list`/`check_diff`) | Real read access to history/diff on the git backend | Done |
| Config hot-reload | Add/remove sources via `config.yaml` without restarting the daemon | In progress |
| MCP tool server | 5 tools (`read/write/create/delete/move_file`) with a real guardrail (elicitation, fail-closed); MCP-triggered edits are actor-attributed via a pending hint the watcher picks up; `write`/`delete` take an optional `expected_version` for optimistic concurrency | Done |
| HTTP API | 3 read-only routes (`/v1/sources`, `/history`, `/diff`), fail-closed 403, `X-API-Key` auth (single shared key, no per-caller scopes yet) | Done |
| CLI | `source list/add/remove`, `history`, `diff`, `rollback`, `rollback-session` (undo everything one actor did, across every watch target) | Done |
| `ctx daemon` | Backgrounds the watch daemon: detached process + PID file, cross-platform graceful stop (`SIGTERM`/`CTRL_BREAK_EVENT`), `start/stop/status` | Done |

## Tech Stack

| Layer | Technology |
| --- | --- |
| Runtime daemon | Python 3.13 · watchdog · homegrown in-process pub/sub, shaped after an AMQP topic exchange (ready to swap in RabbitMQ) |
| Storage | `git` (subprocess) mirror repo per watch target · SQLite (raw `sqlite3`, identity via `st_ino`/`st_dev`) |
| MCP | FastMCP (stdio) |
| HTTP API | FastAPI + Uvicorn (read-only) |
| CLI | Typer |
| Testing | pytest + ruff · GitHub Actions CI (Python 3.10-3.14, ubuntu-latest) |

Per-module detail (data model, concurrency, actor attribution) →
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Install

```bash
pipx install chrono-ctx    # recommended - isolated venv, adds ctx to PATH automatically
```

```bash
pip install chrono-ctx     # also works, but ctx may land outside PATH -
                            # pip will warn if so; ctx itself also warns on
                            # every run until it's fixed
```

```bash
# no install - runs in a cached ephemeral venv. The entry point is `ctx`,
# not `chrono-ctx`, so --from is required (uvx says so if you omit it):
uvx --from chrono-ctx ctx --version
uvx --from chrono-ctx ctx daemon start
```

If `pip`/`ctx` warns `ctx` isn't on PATH, add the printed directory to PATH:

```powershell
# Windows (PowerShell) - opens a new shell for it to take effect
setx PATH "%PATH%;<dir from the warning>"
```

```bash
# Linux/macOS - add to ~/.bashrc or ~/.zshrc to persist across shells
export PATH="$PATH:<dir from the warning>"
```

Requires `git` on PATH (the storage backend shells out to `git`).

## Connect an AI agent (MCP)

The MCP server speaks **stdio** — no URL, no port. Every client below spawns
it as a subprocess, so all you give them is a command.

### 1. The launch command

Whichever runner you already use — both fetch or reuse the package themselves,
so nothing has to be installed first:

```bash
uvx --from chrono-ctx python -m app.mcp.server        # uv
pipx run --spec chrono-ctx python -m app.mcp.server   # pipx
```

The examples below use the `uvx` form; swap in the `pipx run` one anywhere by
replacing the `command`/`args` pair.

### 2. Register it with your client

**Claude Code** — one command, no file editing:

```bash
claude mcp add chrono-ctx -- uvx --from chrono-ctx python -m app.mcp.server
claude mcp add --scope project chrono-ctx -- <command> <args...>   # commit to .mcp.json for the team
```

**Claude Desktop** — `claude_desktop_config.json` (Settings → Developer → Edit
Config; `%APPDATA%\Claude\` on Windows, `~/Library/Application Support/Claude/`
on macOS):

```json
{
  "mcpServers": {
    "chrono-ctx": {
      "command": "uvx",
      "args": ["--from", "chrono-ctx", "python", "-m", "app.mcp.server"]
    }
  }
}
```

**Codex CLI** — `~/.codex/config.toml` (note `mcp_servers`, with an
underscore). Prefer `codex mcp add`, since one malformed line takes down every
server:

```toml
[mcp_servers.chrono-ctx]
command = "uvx"
args = ["--from", "chrono-ctx", "python", "-m", "app.mcp.server"]
```

**VS Code / GitHub Copilot** — `.vscode/mcp.json` (workspace) or the user
profile one. VS Code's top-level key is `servers`, **not** `mcpServers`:

```json
{
  "servers": {
    "chrono-ctx": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "chrono-ctx", "python", "-m", "app.mcp.server"]
    }
  }
}
```

**Cursor** — `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global);
**Windsurf** — `~/.codeium/windsurf/mcp_config.json`
(`%USERPROFILE%\.codeium\windsurf\` on Windows); **Gemini CLI** —
`.gemini/settings.json` (project) or `~/.gemini/settings.json`. All three take
the same `mcpServers` block as Claude Desktop above. Restart the client after
editing.

### 3. Point the agent at the same data as the daemon

The MCP server ignores its cwd on purpose (the client picks it, and it would
silently split the two processes across different `config.yaml` files). It
resolves everything against `CHRONO_CTX_HOME`, falling back to the per-user
data directory.

That default already matches a `pipx`/`pip`-installed daemon, so most setups
need nothing here. **It diverges if the daemon runs from a source checkout**,
which anchors to the repo root instead — and the symptom is quiet: writes land
on disk but nothing is ever versioned. Pin it explicitly in that case:

```json
"env": { "CHRONO_CTX_HOME": "/path/to/chrono-ctx" }
```

### 4. Verify

Start the daemon first (`ctx daemon start`) — the MCP server writes files, the
daemon is what versions them. Then, from the agent, read and write a file
under a watched source and check:

```bash
ctx history <path>          # the write shows up as a rev, attributed to agent:<id>
tail data/mcp.log           # one entry/exit line per tool call, with elapsed time
```

`data/mcp.log` records paths and durations only — never file contents. Every
call is bounded (30s lock wait, 4s per git call, 48s worst case), so a stuck
call fails with a structured error instead of hanging.

## Quick start (from source, for contributing)

```bash
git clone https://github.com/dDxCg/chrono-ctx.git && cd chrono-ctx
uv sync --extra dev              # or: pip install -e ".[dev]"
cp config.example.yaml config.yaml   # declare the sources to watch
```

```bash
ctx daemon start                  # watch + versioning, backgrounded (PID: data/ctx.pid, log: data/ctx.log)
ctx daemon status
ctx daemon stop

uv run python -m app.mcp.server   # MCP server (stdio) — or via fastmcp.json
uv run python -m app.api.server   # HTTP API, read-only (needs HTTP_API_KEY set)

ctx source add <path>
ctx source remove <path>
```

| Service | Address |
| --- | --- |
| HTTP API | http://127.0.0.1:8000 |
| MCP server | stdio, no URL — see [Connect an AI agent (MCP)](#connect-an-ai-agent-mcp) |

| Variable       | Meaning                                     | Default            |
| -------------- | -------------------------------------------- | ------------------ |
| `MODE`         | `dev` or anything else (prod)                 | `dev`              |
| `DATABASE_URL` | SQLite path (set in `.env.dev`/`.env.prod`)   | -                  |
| `CONFIG_PATH`  | Source config file path                       | `config.yaml`      |
| `SCHEMA_PATH`  | SQL schema path                               | `data/schema.sql`  |
| `GIT_REPO_DIR` | Directory holding the git mirror repos         | `data/repo`        |
| `HTTP_API_KEY` | Required `X-API-Key` value for `/v1/*` — unset means every request is rejected | -    |

## Test

```bash
uv run pytest
uv run ruff check .
```

## Docs

| Doc | Content |
| --- | --- |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Detailed architecture: container/sequence diagrams, data model, concurrency, actor attribution, design decisions, open gaps |
| [`docs/runbook-shared-install.md`](docs/runbook-shared-install.md) | Helpdesk-run setup for a shared machine — non-technical users install nothing, technical team owns audit |
