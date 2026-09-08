# 018 — HTTP API key authentication

Status: implemented

## Context

`ARCHITECTURE.md` §8 / the Tier 3 gap list: the HTTP API
(`app/api/server.py`, 3 read-only routes standing since spec
[010](010-audit-http-api.md)) has no auth model at all — any process that
can reach `127.0.0.1:8000` can read every tracked source's full history and
diffs. Fine for a single trusted, same-host caller during development; not
fine the moment the HTTP surface is what a future approval UI or another
system talks to (per `README.md`'s stated target user for this layer).

**Mechanism choice: a single static API key over a request header**, not
OAuth/JWT or mTLS. Reasoning: the HTTP API is a same-host, single-tenant
surface today (one trusted caller, per `README.md`) — the actual gap is
"anyone who can reach the port gets in", not "different callers need
different scopes". A shared secret closes that gap with no new dependency
(`fastapi.security.APIKeyHeader` is already part of FastAPI) and matches
the project's existing env-var-configured convention
(`DATABASE_URL`/`GIT_REPO_DIR`/etc. in `utils/helper.py`). Per-caller scopes
are a materially bigger feature — multiple keys, a mapping to allowed
paths — and not needed yet; noted as a follow-on gap, not built here.

## Scope

**In**
- `utils/helper.py`: `get_http_api_key()`, reading `HTTP_API_KEY` from the
  environment (`.env`/`.env.dev`/`.env.prod`, same `load_dotenv` pattern as
  `get_db_url()`). No default — an unset or empty value means "no valid key
  can ever match", which is the fail-closed behavior this spec wants, not a
  separate branch to code.
- `app/api/deps.py`: `require_api_key` dependency, reads the `X-API-Key`
  request header via `fastapi.security.APIKeyHeader`, compares against
  `get_http_api_key()` with `hmac.compare_digest` (constant-time, avoids a
  timing side-channel on the comparison), raises `HTTPException(401)` on any
  mismatch — missing header, wrong value, or no key configured at all.
- `app/api/router/v1/vcs.py`: the router-level `dependencies=[]` gains
  `Depends(require_api_key)`, so it runs before every route in this router,
  once, rather than being repeated per-route.

**Out**
- Per-caller scopes (multiple keys, each mapped to an allowed path set) —
  noted above, a separate, bigger feature.
- Key rotation, expiry, or any persistence beyond the one env var — matches
  the project's existing single-shared-secret conventions elsewhere
  (there's no user/session/token store anywhere in this codebase).
- Auth on the MCP server or CLI — both already have their own guardrail
  (`app/mcp/guardrail.py`'s scope/elicitation) or run as the trusted local
  operator (CLI); this spec is HTTP-only.

## Acceptance criteria

- AC-1. A request to any `/v1/*` route with no `X-API-Key` header returns
  `401`.
- AC-2. A request with an `X-API-Key` header that doesn't match the
  configured key returns `401`.
- AC-3. A request with an `X-API-Key` header matching the configured key
  succeeds (reaches the route, real status code for that route).
- AC-4. When `HTTP_API_KEY` is unset (or empty) in the environment, every
  request returns `401` — there is no way to "opt out" of auth by leaving
  the key unconfigured.

## Error cases

None beyond AC-1/AC-2/AC-4 above — those *are* the error paths this spec
adds; there's no separate error-shape variation to enumerate.

## Contracts

```python
# utils/helper.py
def get_http_api_key() -> str | None: ...

# app/api/deps.py
def require_api_key(x_api_key: str | None = Security(APIKeyHeader(name="X-API-Key", auto_error=False))) -> None: ...
```

- Header: `X-API-Key: <key>`
- Status codes: `401` (missing/wrong/unconfigured key)

## Non-goals / open questions

None outstanding.
