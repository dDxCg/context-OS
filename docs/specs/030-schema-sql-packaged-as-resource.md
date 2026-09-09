# 030 — ship `schema.sql` as installed package data, fix `init_db()`'s broken path

Status: implemented

## Context

Live-verified 2026-09-09 (same rehearsal that validated spec 029): a real
non-editable install (`uv build` + `pip install` the wheel into a scratch
venv, `PROJECT_ROOT` now correctly resolving per spec 029) still crashes
`ctx daemon start`:

```
FileNotFoundError: SQL script not found at: data/schema.sql
```

Two compounding, independent bugs, both found by tracing the actual
failure, not assumed:

1. **`vcs/services/db.py`'s `init_db()` never calls the existing, correct
   `get_schema_path()` helper (`utils/helper.py:73-74`) at all.** It
   reimplements its own version inline:
   ```python
   SCHEMA_PATH = os.getenv("SCHEMA_PATH", "data/schema.sql")
   db_handler.execute_script(SCHEMA_PATH)
   ```
   A bare, **un-anchored** relative path - resolved against whatever the
   process's `cwd` happens to be, not `PROJECT_ROOT`. This is a
   pre-existing bug independent of packaging: it already breaks today for
   any invocation whose `cwd` isn't the repo root, in a source checkout
   too - it only ever worked because every dev/test/CI invocation happens
   to run from the repo root by convention, never exercised otherwise.
2. **Even a correctly-anchored path wouldn't help a packaged install**:
   `data/schema.sql` lives outside `src/`, so
   `[tool.hatch.build.targets.wheel]`'s `packages = ["src/app", "src/vcs",
   "src/utils"]` never bundles it - the file simply doesn't exist anywhere
   in a `pip install chrono-ctx` install, at any path.

Confirmed empirically (not assumed) that the fix for #2 is cheap:
hatchling already includes **any** file placed inside an already-listed
`packages` directory in the built wheel, `.py` or not, with zero extra
`pyproject.toml` config - verified by placing a probe file at
`src/vcs/db/` and inspecting the built wheel's contents directly.

## Scope

**In**
- Move `data/schema.sql` → `src/vcs/db/schema.sql` (`git mv`, content
  unchanged - a location change, not a schema/DDL change, so `AGENTS.md`
  §4's "account for existing rows" schema-change rule doesn't apply here;
  no row shape changes at all).
- `get_schema_path()` (`utils/helper.py`): resolve via
  `importlib.resources.files("vcs.db") / "schema.sql"` by default -
  schema.sql is part of the *code*, not user data, so (unlike
  `config.yaml`/the SQLite DB file) it has no business being anchored to
  `PROJECT_ROOT` at all; `importlib.resources` finds it correctly via
  Python's normal import machinery regardless of source-checkout vs.
  packaged install. `SCHEMA_PATH` env var, if set, still wins (unchanged
  override contract - explicit still beats every default).
- `init_db()` (`vcs/services/db.py`): call `get_schema_path()` instead of
  its own broken inline reimplementation - deletes the dead
  `os.getenv("SCHEMA_PATH", "data/schema.sql")` line and the unused
  `load_dotenv()`/`import os` it only existed to support.
- Update every hardcoded `"data/schema.sql"` literal (`tests/fixtures/db.py`,
  `tests/unit/app/api/test_auth.py`, `tests/unit/app/api/test_vcs_router.py`,
  `tests/unit/app/cli/test_app.py`, `tests/unit/app/mcp/test_guardrail.py`)
  to the new location - these tests build a raw `sqlite3` connection
  directly rather than going through `init_db()`, so they need the new
  path, not a call-site fix.

**Out**
- Any change to `execute_script()`'s contract
  (`vcs/db/sqlite.py:15-24`) - still takes a filesystem path, still reads
  it directly; `importlib.resources.files()` on a normally-installed
  (non-zipped) package returns a real, readable filesystem path, so no
  in-memory-content special case is needed.
- `reset_db()` - calls `init_db()` internally, inherits the fix for free,
  no separate change.
- `_is_source_checkout()`'s `pyproject.toml` marker (`utils/helper.py`,
  spec 029) - stays; only its secondary `data/schema.sql` marker moves to
  match schema.sql's new location (see Contracts) - `pyproject.toml` alone
  is already a sufficient, sufficient-on-its-own signal.

## Acceptance criteria

- AC-1. `get_schema_path()` with `SCHEMA_PATH` unset returns a real,
  existing file path whose content matches `src/vcs/db/schema.sql`,
  resolved via `importlib.resources` - true both when running from a
  source checkout and (verified live, not just unit-tested) from a real
  non-editable installed wheel.
- AC-2. `get_schema_path()` with `SCHEMA_PATH` explicitly set still
  returns that path (anchored if relative) - unchanged override contract.
- AC-3. `init_db()` on a fresh DB successfully applies the schema
  regardless of the process's current working directory - the actual bug
  (`cwd`-dependent, not `PROJECT_ROOT`-anchored) is what this fixes;
  a regression test must call it from a `cwd` that is **not** the repo
  root and confirm it still works.
- AC-4. A real, live-run smoke test (not unit-mocked): build the wheel,
  install it non-editably into a fresh venv, run `ctx daemon start`
  followed by `ctx daemon status` - the daemon must actually still be
  running (this is the literal check that caught the original bug: it
  reported "started" then died silently).

## Error cases

- EC-1. Given `SCHEMA_PATH` points at a path that doesn't exist,
  `execute_script()`'s existing `FileNotFoundError` behavior is unchanged
  - this spec doesn't touch that contract, only what the *default*
  resolves to.

## Contracts

```python
# utils/helper.py
def get_schema_path() -> str: ...  # same signature, new default resolution

# vcs/services/db.py
def init_db(db_handler: DBHandler) -> None: ...  # same signature, calls get_schema_path()
```

File move: `data/schema.sql` → `src/vcs/db/schema.sql`. No `data/schema.sql`
remains anywhere after this spec - every reference (code, tests, this
repo's own `AGENTS.md`/`README.md` mentions of "schema is `data/schema.sql`")
should point at the new location; doc updates are follow-up, not blocking
this spec's ACs.

## Non-goals / open questions

- Whether `AGENTS.md`/`README.md`'s own prose references to
  `data/schema.sql` need updating - cosmetic doc drift, not code, flagged
  but not required for this spec's ACs to pass.
- Any change to how `DATABASE_URL`/the SQLite DB file itself is located -
  unaffected, still `PROJECT_ROOT`-anchored via spec 029, unrelated to
  schema DDL loading.
