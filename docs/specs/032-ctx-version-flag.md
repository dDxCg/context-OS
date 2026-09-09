# 032 — `ctx --version`

Status: implemented

## Context

No `--version`/`-V` flag exists (`app.py`'s `cli` Typer instance has no
root callback at all). Flagged in
[post-release-hardening-plan.md](../agents/draft/post-release-hardening-plan.md)
Part 3, live-verified: `importlib.metadata.version("chrono-ctx")` (stdlib,
no new dependency) resolves correctly both for a real `pip install` wheel
and for this repo's own `uv sync` editable install (`uv run python -c
"from importlib.metadata import version; print(version('chrono-ctx'))"` →
`0.1.1`) - PEP 660 editable installs still register real dist-info
metadata, so there's no source-checkout special case to handle.

## Scope

**In**
- `app.py`: a `@cli.callback()` with an eager `--version`/`-V` option that
  prints the installed package version and exits 0, before any subcommand
  runs.
- Reads the version via `importlib.metadata.version("chrono-ctx")` - single
  source of truth already established by `pyproject.toml`'s `[project]
  version`, not duplicated anywhere in `src/`.

**Out**
- No change to `pyproject.toml`'s version field or how it's bumped (manual
  bump, per spec 028's decision - unrelated to this spec).
- No `ctx daemon status` or other subcommand gains version output - just
  the root `--version` flag.

## Acceptance criteria

- AC-1. `ctx --version` prints a string containing the installed
  `chrono-ctx` package version and exits with code 0.
- AC-2. `ctx --version` exits before requiring any subcommand - `ctx
  --version` alone (no subcommand) is a complete, valid invocation.
- AC-3. `ctx` (no flags) and `ctx <subcommand>` behave exactly as before -
  adding the callback doesn't change existing command behavior (`ctx` alone
  is currently a usage error, exit code 2 - stays that way).

## Error cases

None - read-only, no new failure mode.

## Contracts

```python
# app.py
@cli.callback()
def main(version: bool = typer.Option(False, "--version", "-V", is_eager=True, callback=...)): ...
```

## Non-goals / open questions

None.
