# AGENTS.md

Operating rules for AI coding agents in this repository.
**Stack:** Python >=3.10 (repo pins 3.13, see `.python-version`) · Typer (CLI) ·
FastMCP (MCP server) · raw `sqlite3` via `vcs/db/sqlite.py` (no ORM, no
migrations — schema is `data/schema.sql`, applied once at init) · pytest ·
ruff. No FastAPI is wired up yet (`app/api/server.py` is a stub, entirely
commented out per README's "chưa triển khai" status) and no mypy is
configured — do not assume either exists until a spec explicitly adds it.

This project follows **strict spec-driven TDD**. Specs come before tests, tests come
before code. An agent that writes implementation code without a failing test in place
has broken the workflow and must back out.

---

## 1. Non-negotiable rules

1. **No production code without a failing test that demands it.**
2. **No test without an approved spec.** Every test maps to a numbered acceptance
   criterion in `docs/specs/`.
3. **Never edit a test to make it pass.** If a test looks wrong, stop and raise it —
   changing the test is a spec change and requires updating `docs/specs/` first.
4. **Never weaken assertions, add `pytest.mark.skip`/`xfail`, loosen type hints, or
   add `# type: ignore` / `# noqa` to get green.** Fix the code or escalate.
5. **One behavior per cycle.** Do not batch five features into one red-green pass.
6. **Do not touch files outside the scope of the current spec** (no drive-by refactors,
   no dependency bumps, no reformatting unrelated files).
7. **Stop and ask** when the spec is ambiguous, contradicts existing behavior, or
   requires a schema/API breaking change.

---

## 2. The workflow

```
SPEC  →  PLAN  →  RED  →  GREEN  →  REFACTOR  →  VERIFY  →  COMMIT
```

### Step 1 — SPEC

Write or update `docs/specs/<NNN>-<slug>.md` **before any code**. Template:

```markdown
# NNN — <Feature name>

## Context
Why this exists. Link to issue/ticket.

## Scope
In scope: ...
Out of scope: ...

## Acceptance criteria
AC-1. Given <state>, when <action>, then <observable outcome>.
AC-2. ...

## Error cases
EC-1. Given <bad input>, the API returns 422 with <shape>.
EC-2. ...

## Contracts
- Endpoint: `POST /v1/things`
- Request / response schema (Pydantic model names + fields)
- Status codes: 201, 409, 422
- Side effects: DB writes, events emitted, external calls

## Non-goals / open questions
```

Rules:
- Every AC must be **observable and testable** — no "should be fast", no "handle
  errors gracefully". Quantify or drop it.
- If you cannot express an AC as an assertion, the spec is not done.
- **Pause here for human review** unless the user explicitly said to proceed straight
  through. Present the spec, then wait.

### Step 2 — PLAN

Post a short plan before touching code:
- Test files to add/modify, one bullet per AC/EC → test name.
- Modules to be created or changed.
- Anything the spec left undecided.

Keep it under ~15 lines. No code in the plan.

### Step 3 — RED

- Write **one** test (or one tightly-related group) for the next AC.
- Name it after the criterion: `test_ac1_creates_thing_and_returns_201`.
- Run it. **It must fail, and fail for the right reason** (assertion/behavior, not
  `ImportError` from a typo). Paste the failure output.
- If a test passes on first run, the test is wrong or the behavior already exists —
  investigate, don't move on.

### Step 4 — GREEN

- Write the **minimum** code to pass that test. No speculative generality, no
  "while I'm here" extras, no unused abstraction layers.
- Run the single test, then the full suite. Both must be green.

### Step 5 — REFACTOR

- Only with a green suite. Clean up naming, duplication, structure.
- Re-run the full suite after each refactor step. Behavior must not change.

### Step 6 — Repeat

Loop steps 3–5 until every AC and EC in the spec has at least one test.

### Step 7 — VERIFY

Run the full gate (see §4) and report actual output. Then self-check:

- [ ] Every AC/EC has a named test; the mapping table in the PR body is filled in.
- [ ] No skipped, xfailed, or commented-out tests were added.
- [ ] Coverage did not drop on the touched modules.
- [ ] No `# type: ignore`, `# noqa`, or `--no-verify` introduced.
- [ ] No files changed outside the spec's scope.
- [ ] `data/schema.sql` updated if the schema changed, and existing-row impact
      is addressed in the spec itself (no separate migration tool exists).

---

## 3. Testing rules

**Layers** — write the cheapest test that can prove the AC:

| Layer | Location | Use for |
|---|---|---|
| Unit | `tests/unit/` | Pure logic, validators, domain rules. No I/O. |
| Integration | `tests/integration/` | Real SQLite (`tests/fixtures/` `db_handler`/`seeder`), real watcher (`watchdog`), real subprocess `git` once `git_store` exists — no Postgres, this project doesn't have one. |
| API/contract | `tests/api/` | **Not applicable yet.** `app/api/server.py` is unimplemented; this layer only exists once a spec stands it up (e.g. [audit-read-api](../docs/specs/) once written to this template) — until then, don't create `tests/api/`. |

**Conventions**

- Arrange–Act–Assert, with blank lines between the three sections.
- One logical assertion per test. Prefer several small tests over one long one.
- Test **behavior through public interfaces**, not private functions or internals.
- No sleeps. No real network. No wall-clock dependence — inject a clock.
- Mock only at true system boundaries (external HTTP, message bus, object storage).
  **Do not mock the code under test**, and do not mock your own repositories in
  integration tests.
- Deterministic data via factories/fixtures, not hand-rolled dicts scattered around.
- Async tests use `pytest.mark.asyncio` (or `asyncio_mode = auto` if configured).
- Every fixed bug starts with a regression test that reproduces it and fails first.

**Prohibited in tests**

```python
# All of these are workflow violations:
@pytest.mark.skip(reason="flaky")          # fix it or delete it
@pytest.mark.xfail                          # unless the spec says the behavior is deferred
assert response.status_code in (200, 201)   # pin the exact contract
assert result is not None                   # assert the actual value
```

---

## 4. Commands

Run these exactly; do not invent alternatives.

```bash
# Environment
uv sync                          # or: pip install -e ".[dev]"

# The gate — both must pass before any commit
ruff check .
pytest -q

# Focused loops
pytest tests/unit/vcs/services/test_x.py::test_ac1_... -x -q
pytest -q --cov=src --cov-report=term-missing
```

There is no `ruff format --check`, `mypy`, or `alembic` step — none of these
are configured in `pyproject.toml` today. Do not invoke them; a spec that
wants to add one is itself a "dependency needs to be added" case under §7,
not something to do inline while implementing an unrelated feature.

If a command is missing or fails to install, **report it — do not silently skip the
gate**.

Schema changes: `data/schema.sql` is applied directly (no migration tool),
via `Initializer` at startup. A schema change has no separate migration step
to write or verify — update `data/schema.sql` and account for existing rows
in the change itself (see [issues.md](../docs/agents/issues.md) for the
project's track record on data that can't be backfilled).

---

## 5. Code conventions

- Type hints on every public function. No `mypy` gate exists to enforce this
  today (see §4) — it's still the convention, just not machine-checked yet.
- Data shapes are `@dataclass` (`vcs/shared/types.py`), not Pydantic — no
  Pydantic dependency exists in this project. Don't introduce one for a
  single feature; that's a §7 "dependency needs to be added" stop-and-ask.
- Business logic lives in `vcs/services/` (`versioning.py`, `configure.py`,
  `audit.py`); data access is raw SQL via `Query` dataclasses executed
  through `DBHandler` (`vcs/db/sqlite.py`) — there is no repository
  abstraction layer to add one to.
- `vcs/workers/` holds the event-driven side (watcher, consumers, the
  pub/sub bus in `bus.py`) — new background/async work goes here, not into
  `vcs/services/`, which stays synchronous and callable directly from CLI,
  MCP tool handlers, or tests alike.
- MCP tool handlers (`app/mcp/server.py`) and CLI commands (`app/cli/app.py`)
  stay thin the same way a router would: parse → call a `vcs/services/`
  function → format the result. Neither framework has a `Depends`-style DI
  container; services are called directly, not injected — module-level
  singletons are already how `vcs/shared/config.py`'s paths work, follow
  that pattern rather than inventing a container.
- Raise domain exceptions from `vcs/services/`; translate to an MCP tool
  error or CLI exit code at the one call site that invokes the service, not
  scattered throughout.
- No secrets, tokens, or real customer data in code, tests, fixtures, or logs.
- Structured logging via `utils.logger` (`@log_enabled` decorator, already
  used throughout `vcs/services/` and `vcs/workers/`) — never log request
  bodies containing credentials or PII.

Layout (actual, under `src/`):

```
app/
  api/          HTTP surface — stub only, not wired up yet
  cli/          Typer commands (app.py)
  mcp/          FastMCP tool server + guardrail.py (scope/elicitation)
vcs/
  services/     business logic — versioning.py, configure.py, audit.py
  workers/      watcher, consumers, bus.py (pub/sub), local/ + config/ subpackages
  db/           sqlite.py — DBHandler, no ORM
  shared/       types.py (dataclasses), config.py (anchored paths)
  adapters/     source-type adapters (local_adapter.py)
utils/          logger.py, helper.py — imported bare (`from utils...`), not `from src...`
docs/
  agents/       design/plan docs (issues.md, *-plan.md) — narrative, prose-linked
  specs/        NNN-slug.md, one per feature, this file's template — AC/EC-driven
tests/
  unit/ integration/ fixtures/ conftest.py   (no tests/api/ until app/api/ is real)
```

`docs/agents/` and `docs/specs/` are not duplicates — `docs/agents/` holds
the exploratory/narrative design docs this project already had (why a
decision was made, what was tried, cross-referenced by prose), `docs/specs/`
is this file's strict AC/EC contract format. A `docs/agents/*-plan.md` doc
earns a `docs/specs/NNN-slug.md` once someone is about to implement it under
this workflow — not before, and not automatically.

---

## 6. Commits and PRs

- Conventional commits: `feat:`, `fix:`, `test:`, `refactor:`, `chore:`, `docs:`.
- Commit the failing test and its implementation together, or as a `test:` commit
  immediately followed by the `feat:`/`fix:` commit — never implementation alone.
- Never `git commit --no-verify`. Never force-push a shared branch.
- Do not commit, push, or open a PR unless the user asked for it.

PR body must include:

```markdown
Spec: specs/NNN-slug.md

| Criterion | Test |
|---|---|
| AC-1 | tests/api/test_things.py::test_ac1_... |
| EC-1 | tests/api/test_things.py::test_ec1_... |

Gate: ruff ✅  pytest ✅ (N passed)
```

---

## 7. When to stop and ask

Halt and surface the question instead of guessing:

- The spec is ambiguous, or two ACs conflict.
- Making the test pass would require changing an existing test.
- The change breaks a public API contract or requires a destructive migration.
- A dependency needs to be added or upgraded.
- The suite was already red before you started.
- You have been stuck on the same failing test for three attempts — report what you
  tried and what you observed, rather than trying a fourth workaround.

Reporting a blocker is a successful outcome. Silently relaxing a test is not.