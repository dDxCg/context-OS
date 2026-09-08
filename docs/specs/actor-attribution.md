# Spec — actor attribution on versions

Status: **proposed, not started.** Implements
[cowork-enterprise-plan.md](../cowork-enterprise-plan.md) Phase 3, items 1–2.
Depends on [git-backend-plan.md](../git-backend-plan.md) landing first — this
spec populates git's commit author/message fields, it doesn't create new
storage.

## Identity model

Three sources of a change, three identity shapes:

| Origin | Identity string | Captured where |
|---|---|---|
| MCP tool call | `agent:<mcp-session-id>` | `Context` object already passed into `ensure_scope(ctx, path)` ([guardrail.py](../../../src/app/mcp/guardrail.py)) |
| `ctx` CLI command | `cli:<os-username>` | `os.getlogin()`, overridable via `CTX_ACTOR` env var for scripted/service invocations |
| Direct filesystem edit (no MCP/CLI call — watchdog-only) | `unknown:filesystem` | Nowhere to capture anything better; see "Known gap" below |

`unknown:filesystem` is a deliberate, honest default, not a workaround. This
project's watcher deliberately catches out-of-band edits a tool-call-only
system like Claude Code's checkpointing can't (per
[competitive-landscape.md](../competitive-landscape.md)) — the cost of that
broader capture surface is that some edits arrive with no actor context at
all. Do not try to infer identity from file ownership/`st_uid`; on a shared
dev machine or a service account that's misleading, not just imprecise.

## Propagation

Add an optional field to the event dataclasses in
[types.py](../../../src/vcs/shared/types.py):

```python
@dataclass
class SourceEvent:
    src: str
    type: str
    provider: str = "local"
    is_dir: bool = False
    actor: str | None = None   # new
```

- `guardrail.py`'s `ensure_scope` gains the actor string from `ctx` and
  threads it through `ScopeGrant.commit()` into `add_sources`, and the MCP
  tool handlers ([server.py](../../../src/app/mcp/server.py), once wired per
  [git-backend-plan.md](../git-backend-plan.md)) attach it to whatever event
  the write produces.
- `ctx` CLI commands construct events (or call the equivalent git-store
  functions) with `actor="cli:<user>"` directly.
- Watchdog-originated events (`Handler.on_any_event` in
  [local_watcher.py](../../../src/vcs/workers/local/local_watcher.py)) leave
  `actor=None`. `git_store`'s commit function defaults `None` to
  `"unknown:filesystem"` at the point of commit, not earlier — keeps the
  default in one place.
- `event_to_dict`/`event_from_dict` ([types.py](../../../src/vcs/shared/types.py))
  already serialize whatever fields exist; add `actor` to both, matching the
  pattern used for `is_dir`. This is also what makes actor identity survive
  the trip across the message bus in
  [central-writer-distribution.md](central-writer-distribution.md) — an edit
  made on a remote edge server must still attribute correctly at the central
  writer.

## Git commit shape

```python
def commit(repo_path: Path, message: str, actor: str) -> str:
    name, _, rest = actor.partition(":")
    author = f"{actor} <{name}@chrono-ctx.local>"   # synthetic, stable, sortable by origin type
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", f"--author={author}", "-m", message],
        check=True,
    )
```

Synthetic email domain (`@chrono-ctx.local`) rather than a real address —
this is an attribution label, not a mailer target, and a real-looking email
in commit metadata would be misleading.

## Commit message template

Mechanical, generated at the same call site that sets the author — no
separate design needed:

```
{verb} {relpath} via {actor}
```

e.g. `modified docs/api.md via agent:sess-9f3a`,
`created 3 files via config approval (agent:sess-9f3a)` for a batched
config-driven ingest (one commit per debounce window, per
[git-backend-plan.md](../git-backend-plan.md)'s commit-granularity design —
the message summarizes everything that commit's diff touched, not just one
file).

## Known gap

A watched directory edited entirely by hand (no agent ever involved) produces
a git history that's 100% `unknown:filesystem` commits. That's correct, not
a bug — this spec doesn't invent a false identity to fill it in. If this
turns out to matter in practice (e.g., an enterprise requirement to
attribute filesystem edits to the OS session that made them), that's a
separate, larger spec — likely requiring an OS-level audit hook, out of reach
of a Python filesystem watcher, and explicitly out of scope here.

## Tests

- `tests/unit/vcs/shared/test_types.py` — `actor` round-trips through
  `event_to_dict`/`event_from_dict`; defaults to `None` when absent (backward
  compatible with events serialized before this field existed).
- `tests/unit/vcs/services/test_git_store.py` (once it exists, per
  [git-backend-plan.md](../git-backend-plan.md)) — `commit()` sets the
  expected `--author`; `None` actor falls back to `unknown:filesystem`.

## Verification

```powershell
uv run pytest -q
```

Live: approve a file via MCP guardrail, edit it via `ctx` CLI, then hand-edit
a sibling directly — `git log --format='%an %s'` on the mirror repo shows
three distinct, correctly-labeled origins.
