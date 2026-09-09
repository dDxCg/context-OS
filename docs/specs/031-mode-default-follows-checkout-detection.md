# 031 — `MODE` default follows source-checkout detection, not a hardcoded "dev"

Status: implemented

## Context

Live-caught 2026-09-09: `get_db_url()` (`utils/helper.py`) defaults `MODE`
to `"dev"` unconditionally (`os.getenv("MODE", "dev")`), which picks
`data/db-dev.sqlite` as the default DB file. That default made sense when
the only way to run chrono-ctx was a source checkout (a contributor's own
dev machine, `.env.dev` present) - but spec 029/030 just made a real,
non-editable `pip install chrono-ctx` a first-class supported path, and
that install has no `.env.dev`, no reason to be "dev mode" by default, and
every real end-user deploy of it is conceptually production use, not
chrono-ctx-the-project's own development. Confirmed by grep: `MODE` is
read in exactly one place (`get_db_url()`); `DEBUG` (also declared in the
gitignored `.env.dev`/`.env.prod`) is never read anywhere in `src/` at all
- dead config, unrelated to this fix.

## Scope

**In**
- `utils/helper.py`: `MODE`'s default becomes conditional on the same
  source-checkout detection spec 029 already computes for `PROJECT_ROOT`
  (`_is_source_checkout()`) - `"dev"` when running from a real source
  checkout (unchanged behavior for every existing contributor/CI flow),
  `"prod"` when running from a packaged (non-editable) install.
  `MODE` env var, if explicitly set, still wins over either default -
  unchanged override contract, same shape as every other env-overridable
  value in this file.

**Out**
- `DEBUG` - unused anywhere in `src/`, not touched.
- Any change to `_resolve_project_root()`/`PROJECT_ROOT` itself - reuses
  the existing source-checkout signal, doesn't recompute or alter it.
- Any change to `.env.dev`/`.env.prod`'s own content - still valid,
  gitignored, dev-machine-only override files; only the *default* used
  when neither is present changes.

## Acceptance criteria

- AC-1. Given a source checkout (a `pyproject.toml` marker present) and
  `MODE` unset, `get_db_url()` still defaults to `data/db-dev.sqlite` -
  unchanged behavior, no regression for existing dev/CI flows.
- AC-2. Given a packaged (non-source-checkout) install and `MODE` unset,
  `get_db_url()` defaults to `data/db.sqlite`, not `data/db-dev.sqlite`.
- AC-3. Given `MODE` is explicitly set (either shape of install), that
  value still wins - e.g. `MODE=dev` in a packaged install still resolves
  `data/db-dev.sqlite`, and `MODE=prod` in a source checkout still
  resolves `data/db.sqlite`.

## Error cases

None - a default-selection change, no new failure mode.

## Contracts

```python
# utils/helper.py
def get_db_url() -> str: ...  # same signature; default MODE now checkout-aware
```

## Non-goals / open questions

- Whether "prod" is the right word/concept for a single-user packaged
  install at all (vs. just "not dev") - out of scope; reusing the
  existing `MODE`/`.env.prod` vocabulary this codebase already has rather
  than inventing a third state.
