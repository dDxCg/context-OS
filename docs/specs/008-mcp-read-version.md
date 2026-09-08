# 008 — MCP `read_file` exposes real git version

Status: implemented

## Context

`app/mcp/server.py`'s `read_file` tool has always returned a hardcoded
`"version": None`. The git mirror backend (specs 001-007) now tracks real
commit history for every path under a watch target, but nothing reads that
history back out through the MCP surface. This is the smallest, cleanly
scoped slice of "MCP wiring": read-only, additive, and outside the
deliberate write-path boundary documented in `server.py` (MCP write tools do
plain filesystem I/O only; the watcher is the sole writer into git, to avoid
double-processing the same change - see the module docstring comment above
`read_file`). Reading the current head rev carries none of that
double-processing risk.

## Scope

**In**
- A new `versioning.current_version(path, watch_targets=None) -> str | None`
  helper: resolves `path`'s mirror location and returns its HEAD rev, or
  `None` if it has no commit history yet or isn't under any watch target.
- `read_file` calls it and returns the real value instead of the hardcoded
  `None`.

**Out**
- Any other MCP tool (`write_file`, `create_file`, `delete_file`,
  `move_file`) - unchanged, still plain filesystem I/O per the existing
  design.
- Actor attribution for MCP-triggered edits (correlating an MCP write with
  the watcher event it causes) - a separate, harder design problem, not
  addressed here.
- `vcs/services/audit.py` - still stubs, untouched.

## Acceptance criteria

- AC-1. Given a path whose mirror already has commit history, `read_file`
  returns `version` equal to that path's current `git_store.head_rev(...)`.
- AC-2. Given a path with no commit history yet in its mirror (never
  written through `git_store` - true for every MCP-only write today, since
  those bypass git entirely), `read_file` returns `version: None`, same as
  today.

## Error cases

- EC-1. If `path` cannot be resolved to any watch target
  (`PathNotWatchedError`), `read_file` still succeeds; `version` is `None`
  rather than the read failing.

## Contracts

```python
# vcs/services/versioning.py
def current_version(path: str, watch_targets: list[str] | None = None) -> str | None:
    """HEAD rev of path in its mirror repo, or None if it has no commit
    history yet, or isn't under any watch target."""
```

- `app/mcp/server.py`'s `read_file` return dict gains a real `version` via
  this helper; signature unchanged.

## Non-goals / open questions

- None outstanding. Existing tests in `tests/unit/app/mcp/test_guardrail.py`
  assert `version: None` for paths written only via MCP tools (never
  through `git_store`), which stays correct after this change - no existing
  test needs modification.
