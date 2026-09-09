# 029 — packaged-install data directory fallback (`anchored()` + `DATABASE_URL`)

Status: implemented

## Context

Live-verified 2026-09-09 via the actual PyPI publish (spec 028): `pip
install chrono-ctx` succeeds, but running `ctx daemon start` from that
install would break, for **two** compounding reasons - one already
predicted in
[draft/fast-install-fast-start-shipping-plan.md](../agents/draft/fast-install-fast-start-shipping-plan.md)
Part 3, one newly found while grounding this spec:

1. **`anchored()` (`utils/helper.py:13,16-26`)**: `PROJECT_ROOT =
   Path(__file__).resolve().parents[2]` - three directories up from
   wherever `utils/helper.py` physically sits. In a source checkout that's
   the repo root; in a real `pip install` it's
   `site-packages/utils/helper.py`, so `parents[2]` resolves to something
   like the venv's `lib/pythonX.Y/` - every relative path (`DATABASE_URL`,
   `CONFIG_PATH`, `GIT_REPO_DIR`, `SCHEMA_PATH`) breaks.
2. **`DATABASE_URL` has no code-level default at all** (`get_db_url()`,
   `utils/helper.py:33-41`): `db_url = os.getenv("DATABASE_URL")` returns
   `None` unless a `.env.dev`/`.env.prod` file supplies it - and those
   files are gitignored, dev-machine-only, **never shipped in the wheel**.
   `DBHandler.from_url(None)` (`sqlite3.connect(None, ...)`) crashes
   immediately - every source-checkout dev has one of these files sitting
   locally already (undocumented assumption), but a packaged install has
   no such file and never will. This is a second, independent blocker for
   the exact same `pip install chrono-ctx && ctx daemon start` flow -
   found by tracing the actual failure path, not assumed.

Both block the same target flow and share the same fix shape: give the
packaged-install case a real default instead of silently requiring
dev-only local files.

## Scope

**In**
- `utils/helper.py`: `PROJECT_ROOT` becomes a resolved value, not a bare
  `parents[2]` literal, via a 3-step order:
  1. `CHRONO_CTX_HOME` env var, if set - explicit override, resolves the
     multi-project-collision question the draft left open: two
     independent `ctx daemon start` invocations on one machine stay
     separate only if the operator sets this per project; no automatic
     namespacing is attempted.
  2. Source-checkout detection - if `pyproject.toml` or
     `data/schema.sql` exists at the `parents[2]` candidate, use it
     (today's behavior, unchanged for `uv sync`/`pip install -e .`).
  3. Packaged-install fallback - an OS-appropriate per-user data
     directory, **hand-rolled** (`sys.platform` branching - Windows
     `%LOCALAPPDATA%`, macOS `~/Library/Application Support`, Linux
     `$XDG_DATA_HOME`/`~/.local/share`, all `/chrono-ctx`), not a new
     `platformdirs` dependency - three branches doesn't justify one, and
     this project already does the same kind of `sys.platform` branching
     directly (`daemon.py`'s POSIX/Windows split).
- `get_db_url()`: `DATABASE_URL` gets a real default
  (`data/db-dev.sqlite` in `MODE=dev`, `data/db.sqlite` otherwise -
  matching the filenames the gitignored `.env.dev`/`.env.prod` already
  used, so existing dev checkouts see no behavior change) instead of
  returning `None` when unset.

**Out**
- `platformdirs` as a dependency - considered, not added (see above).
- Any change to per-variable overrides (`SCHEMA_PATH`, `CONFIG_PATH`,
  `GIT_REPO_DIR`, `SNAPSHOT_DIR` env vars) - already correct, already
  override-first; only the *fallback* they anchor against when unset is
  changing.
- Migrating/moving an existing source-checkout install's data - this only
  changes what happens when no marker is found at all; a real checkout
  keeps resolving to itself exactly as before.
- The `ctx daemon start` git-missing preflight or least-privilege
  first-run prompt (shipping plan Parts 1/5) - separate, unrelated specs.

## Acceptance criteria

- AC-1. Given `CHRONO_CTX_HOME` is set, `PROJECT_ROOT` equals that path
  (resolved, `~` expanded) regardless of where `utils/helper.py` physically
  lives.
- AC-2. Given `CHRONO_CTX_HOME` is unset and a `pyproject.toml` (or
  `data/schema.sql`) exists at the `parents[2]` candidate,
  `PROJECT_ROOT` equals that candidate - unchanged behavior for every
  existing source-checkout dev/CI flow.
- AC-3. Given `CHRONO_CTX_HOME` is unset and no marker exists at the
  `parents[2]` candidate (the packaged-install shape), `PROJECT_ROOT`
  equals the OS-appropriate per-user data directory, suffixed
  `/chrono-ctx`.
- AC-4. `get_db_url()` with `MODE=dev` and no `DATABASE_URL` set (no
  `.env.dev` present) returns `<PROJECT_ROOT>/data/db-dev.sqlite`, not
  `None`.
- AC-5. `get_db_url()` with `MODE` unset-or-anything-else and no
  `DATABASE_URL` set returns `<PROJECT_ROOT>/data/db.sqlite`, not `None`.
- AC-6. `get_db_url()` with `DATABASE_URL` explicitly set still returns
  that value (anchored if relative) - AC-4/AC-5's default never
  overrides an explicit setting, matching every other env-overridable
  path in this codebase.

## Error cases

- EC-1. Given the packaged-install fallback directory doesn't exist yet
  on disk, `PROJECT_ROOT` still resolves to it (a path, not a
  requirement that it pre-exist) - `create_dirs()`/`init_config_file()`
  (already called at `Initializer` construction/`init()`) are what
  actually create it, unchanged.

## Contracts

```python
# utils/helper.py
PROJECT_ROOT: Path  # now resolved via CHRONO_CTX_HOME -> source-checkout
                     # marker -> per-user data dir, not a bare parents[2]

def anchored(value: str) -> str: ...  # unchanged signature/behavior
def get_db_url() -> str: ...          # now never returns None
```

No signature changes anywhere - `PROJECT_ROOT` stays a module-level
`Path` constant, every caller (`vcs/shared/config.py`,
`vcs/initialize.py`, `vcs/runtime.py`, etc.) is unaffected by construction.

## Non-goals / open questions

- `CHRONO_CTX_HOME` as the exact env var name - open for confirmation
  before implementation; anything equally clear works, this is the
  proposed default.
- Whether the packaged-install fallback directory should be created and
  seeded with a default `config.yaml` automatically, vs. left for the
  `ctx daemon start` first-run prompt (shipping plan Part 1) to handle -
  Part 1 already owns "no `config.yaml` yet," this spec only fixes
  *where* things go, not first-run UX.
