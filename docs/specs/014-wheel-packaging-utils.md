# 014 — Wheel packaging omits `src/utils`

Status: implemented

## Context

[issues.md #15](../agents/issues.md): `pyproject.toml`'s
`[tool.hatch.build.targets.wheel]` lists `packages = ["src/app", "src/vcs"]`
only. `utils` is a third top-level package, imported unconditionally on
every startup path (`vcs/runtime.py`, `vcs/initialize.py`,
`vcs/services/configure.py`, `vcs/shared/config.py`). A real wheel install
(not the editable dev install this repo always runs under) installs the
`ctx` console script and it dies with `ImportError: No module named 'utils'`
on the first command.

## Scope

**In**
- `pyproject.toml`: add `"src/utils"` to the wheel packages list.

**Out**
- Actually building/installing a wheel in CI — no `build`/`installer`
  dependency exists in this project and adding one is a separate
  stop-and-ask (§7). The regression test instead pins the packages list
  itself, which is the actual defect surface.

## Acceptance criteria

- AC-1. `pyproject.toml`'s `[tool.hatch.build.targets.wheel]` `packages` list
  contains `"src/utils"` alongside `"src/app"` and `"src/vcs"`.

## Error cases

None — this is a static config fix, no runtime branch.

## Contracts

- File: `pyproject.toml`
- Config key: `[tool.hatch.build.targets.wheel].packages`

## Non-goals / open questions

None outstanding.
