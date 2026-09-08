# Spec — read-only audit surface, driveable outside an MCP session

Status: **proposed, not started.** Implements
[cowork-enterprise-plan.md](../cowork-enterprise-plan.md) Phase 3, item 3.
Prerequisite for [conflict-ux.md](conflict-ux.md) and for any future
human-facing approval surface — neither can be built while `history`/`diff`
are only reachable from inside a live MCP session or the local CLI process.

## Why this is its own spec

`vcs/services/audit.py` (implemented against git per
[git-backend-plan.md](../git-backend-plan.md)) is a Python function surface —
callable from the CLI process or an MCP tool handler, both of which run
in-process with the runtime. A non-technical approval surface (Phase 3 in
[cowork-enterprise-plan.md](../cowork-enterprise-plan.md)) is necessarily a
*separate* process — a notification service, a small web view, whatever gets
built later — and needs a way to ask "what changed" without being a Python
import of this codebase. That's an HTTP surface, not a new capability.

## Scope: read-only, three endpoints

Reuse the already-stubbed [app/api/server.py](../../../src/app/api/server.py) /
[router/v1/vcs.py](../../../src/app/api/router/v1/vcs.py) — currently entirely
commented out per the README's "chưa triển khai" status. This spec is what
un-comments it, scoped narrowly:

| Method | Path | Backing function |
|---|---|---|
| `GET` | `/v1/sources` | `audit.get_sources` |
| `GET` | `/v1/history/{path}` | `audit.get_version_list` |
| `GET` | `/v1/diff/{path}?v1=&v2=` | `audit.check_diff` |

**No write endpoint in this spec.** `rollback_source` is deliberately not
exposed here — a write surface reachable over HTTP is a materially bigger
security decision (auth, CSRF-equivalent concerns, rate limiting) than this
spec's scope. If Phase 3's approval surface ends up needing to trigger a
rollback remotely, that's a follow-up spec, not an extension of this one.

## Enforcement — same rule as every other audit entrypoint

Every handler calls the read-only scope check established in the
`phân quyền` design discussion behind
[cowork-enterprise-plan.md](../cowork-enterprise-plan.md): fail-closed,
**no elicitation** — an HTTP caller asking for history of a file it can't
prove it's scoped to gets `403`, full stop, no path to silently regain access
the way an MCP tool call can elicit approval for a *new* grant. This surface
must not become a side door around the guardrail.

```python
def check_scope_readonly(path: str) -> bool:
    return is_path_in_scope(path)   # reuse configure.is_path_in_scope directly, no elicit branch
```

## Auth — deliberately unresolved here

Every request needs a caller identity to (a) drive `check_scope_readonly`
against *that caller's* granted scope, not global scope, and (b) populate
`actor` per [actor-attribution.md](actor-attribution.md) if this surface ever
grows a write path. Both require a caller-identity model this repo doesn't
have yet (`config.yaml`'s sources are scoped to the whole local runtime, not
per-caller). **Open question, not answered by this spec:** whether scope
stays global-per-runtime (simplest, matches today) or grows a per-caller
grant model (needed for real multi-tenant enterprise use, per
[cowork-enterprise-plan.md](../cowork-enterprise-plan.md)'s "Not done here").
Until decided, this surface assumes a single trusted caller (e.g., a
same-host notification service) and enforces scope globally, same as the CLI
does today.

## Response shape

Match the CLI's planned `--json` output format
([cli-plan.md](../cli-plan.md) §2) rather than inventing a parallel schema —
both consume the same `audit.py` return values.

```json
// GET /v1/history/docs/api.md
{
  "path": "docs/api.md",
  "versions": [
    {"rev": "a1b2c3d", "created_at": "...", "actor": "agent:sess-9f3a", "message": "modified docs/api.md via agent:sess-9f3a"}
  ]
}
```

## Files

| File | Change |
|---|---|
| [app/api/server.py](../../../src/app/api/server.py) | un-comment, wire the three routes |
| [app/api/router/v1/vcs.py](../../../src/app/api/router/v1/vcs.py) | implement handlers calling `audit.py` |
| `vcs/services/audit.py` | add `check_scope_readonly`, call it first in every function (also fixes the `phân quyền` gap flagged against `history`/`diff`/`rollback` generally) |

## Tests

- `tests/unit/app/api/test_vcs_router.py` — new. `TestClient` (FastAPI) against
  each route; assert `403` for an out-of-scope path with **no** elicitation
  side effect (config.yaml unchanged after the call — the regression this
  spec exists to prevent).

## Verification

```powershell
uv run pytest -q
uv run python -m app.api.server &
curl http://localhost:8000/v1/history/docs/api.md
curl http://localhost:8000/v1/history/some/never-approved/path.md   # expect 403, config.yaml unchanged
```

## Not done here

- Any write endpoint (`rollback` over HTTP).
- Per-caller auth / multi-tenant scope model — flagged as open, not decided.
- The actual human-facing UI that consumes this API — this spec is the API
  only, per [cowork-enterprise-plan.md](../cowork-enterprise-plan.md)'s
  "Not done here."
