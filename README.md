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
| MCP tool server | 5 tools (`read/write/create/delete/move_file`) with a real guardrail (elicitation, fail-closed); MCP-triggered edits are actor-attributed via a pending hint the watcher picks up | Done |
| HTTP API | 3 read-only routes (`/v1/sources`, `/history`, `/diff`), fail-closed 403, no per-caller auth yet | In progress |
| CLI | `source list/add/remove`, `history`, `diff`, `rollback` use git-rev strings throughout | Done |

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

## Quick start

```bash
git clone https://github.com/dDxCg/chrono-ctx.git && cd chrono-ctx
uv sync                          # or: pip install -e ".[dev]"
cp config.example.yaml config.yaml   # declare the sources to watch
```

Requires `git` on PATH (the storage backend shells out to `git`).

```bash
uv run python -m vcs.runtime      # daemon: watch + versioning
uv run python -m app.mcp.server   # MCP server (stdio) — or via fastmcp.json
uv run python -m app.api.server   # HTTP API, read-only

ctx source add <path>
ctx source remove <path>
```

| Service | Address |
| --- | --- |
| HTTP API | http://127.0.0.1:8000 |
| MCP server | stdio, no URL — register via `fastmcp.json` with an MCP client |

| Variable       | Meaning                                     | Default            |
| -------------- | -------------------------------------------- | ------------------ |
| `MODE`         | `dev` or anything else (prod)                 | `dev`              |
| `DATABASE_URL` | SQLite path (set in `.env.dev`/`.env.prod`)   | -                  |
| `CONFIG_PATH`  | Source config file path                       | `config.yaml`      |
| `SCHEMA_PATH`  | SQL schema path                               | `data/schema.sql`  |
| `GIT_REPO_DIR` | Directory holding the git mirror repos         | `data/repo`        |

## Test

```bash
uv run pytest
uv run ruff check .
```

## Docs

| Doc | Content |
| --- | --- |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Detailed architecture: container/sequence diagrams, data model, concurrency, actor attribution, design decisions, open gaps |
