# 010 — HTTP read surface over `audit.py`

Status: implemented

## Context

[audit-read-api.md](audit-read-api.md) (narrative draft) specifies a
read-only HTTP surface over `audit.py`, needed so a future non-technical
approval surface (Phase 3, [cowork-enterprise-plan.md](../agents/cowork-enterprise-plan.md))
can ask "what changed" without importing this codebase. Spec 009 built the
`audit.py` functions this surface calls. This spec wires the HTTP layer,
approved with the new dependency it requires: `fastapi` (+ `uvicorn` to
serve it — already present transitively via `fastmcp`, now promoted to a
direct dependency) added to `pyproject.toml`.

`src/app/api/server.py` is currently 100% commented out; `router/v1/vcs.py`
is an unused bare `APIRouter`. Neither has ever been runnable.

## Scope

**In**
- `fastapi`/`uvicorn` added to `pyproject.toml` dependencies.
- `app/api/server.py`: real `FastAPI()` app, CORS middleware (as originally
  commented), mounts the v1 router, `main()` entrypoint via `uvicorn.run`.
- `app/api/router/v1/vcs.py`: three `GET` routes per audit-read-api.md's
  table, backed by spec 009's `audit.py` functions.
- Fail-closed scope enforcement: `OutOfScopeError` (spec 009) → HTTP 403,
  no elicitation — matches audit-read-api.md's "no side door around the
  guardrail" rule.

**Out**
- Any write endpoint (`rollback` over HTTP) — same reasoning
  audit-read-api.md already gives.
- Per-caller auth / multi-tenant scope model — audit-read-api.md leaves
  this open; this spec keeps today's single-trusted-caller, global-scope
  assumption (same as the CLI).
- The human-facing UI that would consume this API.

## Design deviation from the draft

audit-read-api.md writes the path routes as `/v1/history/{path}` and
`/v1/diff/{path}?v1=&v2=`, with the filesystem path embedded as a URL path
segment. A real source path can contain `/`, and on Windows a drive letter
and `:` (`C:\Users\...`) — awkward and ambiguous to URL-encode as a single
path segment, and unlike `/v1/sources` there's no reason it needs to look
REST-resource-ish. This spec uses a `path` **query parameter** instead on
both routes. Everything else (methods, response shapes, scope enforcement)
follows the draft as written.

## Acceptance criteria

- AC-1. `GET /v1/sources` returns 200 and the JSON list `audit.get_sources`
  produces.
- AC-2. `GET /v1/history?path=...` for an in-scope path returns 200 with
  `{"path": path, "versions": [...]}`, `versions` being
  `audit.get_version_list`'s result.
- AC-3. `GET /v1/diff?path=...&v1=...&v2=...` for an in-scope path returns
  200 with `{"path": path, "v1": v1, "v2": v2, "diff": text}`, `diff` being
  `audit.check_diff`'s result.

## Error cases

- EC-1. `GET /v1/history` or `/v1/diff` for a path outside the current
  config scope returns 403, and does not call git (no info about whether
  the path even has history leaks into the response body).

## Contracts

```python
# app/api/router/v1/vcs.py
router = APIRouter(prefix="/v1")

@router.get("/sources")
def list_sources(db_handler: DBHandler = Depends(get_db_handler)) -> list[dict]: ...

@router.get("/history")
def history(path: str) -> dict: ...   # 403 via HTTPException on OutOfScopeError

@router.get("/diff")
def diff(path: str, v1: str, v2: str) -> dict: ...   # 403 via HTTPException on OutOfScopeError

# app/api/server.py
def get_db_handler():
    """FastAPI dependency: yields a DBHandler, closes it after the request."""

app = FastAPI()   # CORS middleware, includes router
def main(): ...    # uvicorn.run(app, ...)
```

## Tests

- `tests/unit/app/api/test_vcs_router.py` — new. FastAPI `TestClient`
  against each route; `get_db_handler` overridden via
  `app.dependency_overrides` to the test's in-memory `db_handler` fixture,
  same pattern `test_guardrail.py` uses for the MCP surface.

## Non-goals / open questions

- None outstanding.
