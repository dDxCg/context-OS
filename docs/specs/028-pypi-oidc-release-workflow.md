# 028 — tag-triggered PyPI release via OIDC trusted publishing

Status: implemented

## Context

[draft/fast-install-fast-start-shipping-plan.md](../agents/draft/fast-install-fast-start-shipping-plan.md)
Part 4, approved: PyPI only (no npm/choco/brew/apt), OIDC trusted
publishing (no `PYPI_API_TOKEN` secret). The PyPI project's pending
publisher is already registered (repo owner/name, workflow filename
`cd.yaml`, no GitHub Environment restriction). No release/publish workflow
exists today - `.github/workflows/ci.yaml` only runs `ruff`/`pytest` on
push/PR, nothing builds or publishes. Version is manually bumped in
`pyproject.toml` before each tag (decided over `hatch-vcs`/dynamic
versioning - no new build-dependency, matches `AGENTS.md` §7's
don't-silently-add-a-dependency rule).

## Scope

**In**
- `.github/workflows/cd.yaml` (filename fixed - must match what's already
  registered with PyPI's pending publisher; a different filename means the
  OIDC exchange is rejected, not a naming preference).
- `.github/workflows/ci.yaml`: add `v*.*.*` to its existing `push.tags`
  trigger (alongside `push.branches`/`pull_request`, both unchanged) - a
  tag push previously didn't run CI at all.
- Trigger for `cd.yaml`: `workflow_run` on CI's completion, gated to only
  continue when that run (a) concluded `success`, (b) was itself triggered
  by a `push` (not a PR), and (c) was a tag push (`head_branch` starting
  with `v`) - **not** a direct `push: tags` trigger on `cd.yaml` itself.
  CD must depend on CI having actually passed for that exact commit, not
  merely on a tag existing - "CI chạy trước, nếu pass thì trigger CD."
- Job 1 (build + smoke test): `uv build` (wheel + sdist), then install the
  built **wheel** (not editable, not sdist) into a fresh venv and run a
  smoke command, proving the actual artifact about to be published
  installs and runs - not just that `pyproject.toml`'s config is
  syntactically fine (the exact gap spec 014 left open: "actually
  building/installing a wheel in CI" was explicitly out of scope there).
- Job 2 (publish): depends on Job 1 succeeding, same workflow run, same
  tag. `id-token: write` permission, `uv publish` - no `environment:` key
  (none was registered with the pending publisher, so none is enforced or
  expected).
- A pre-publish guard: the tag's version (`vX.Y.Z` minus the leading `v`)
  must equal `pyproject.toml`'s `version` field - refuses to publish on
  mismatch, catching "tagged but forgot to bump `pyproject.toml`" before
  it becomes an unfixable published version (PyPI never allows
  re-uploading the same version number, even a broken one).

**Out**
- `hatch-vcs`/dynamic versioning - decided against this pass (see
  Context).
- TestPyPI dry-run automation - a manual rehearsal before the very first
  real tag is recommended operationally, not built as a second workflow
  here (no separate TestPyPI pending-publisher registration exists yet
  either - a human step, not this spec's scope).
- Any non-PyPI channel (npm/choco/brew/apt) - explicitly out per the
  draft's decision.
- Any change to `ci.yaml`'s existing `test` job/matrix - only its trigger
  list gains one entry (`push.tags`), nothing about what it runs changes.
- The `ctx daemon start` git-missing preflight, the `anchored()` packaged-
  install fallback, or the least-privilege first-run prompt - separate
  drafts/specs (Parts 1, 3, 5 of the shipping plan), not this one.

## Acceptance criteria

Not pytest-verifiable - a GitHub Actions workflow has no test harness in
this project's stack (`AGENTS.md`'s testing-layers table has no "CI
workflow" row). Verification is structural (the file matches every AC
below, read back after writing) plus one real, human-run rehearsal before
the first tag:

- AC-1. `cd.yaml`'s jobs only proceed when the triggering CI run (a)
  concluded `success`, (b) was itself triggered by a `push` (not a PR),
  and (c) was a tag push (`v*.*.*`, checked via `head_branch` starting
  with `v`) - a failed or non-tag CI run, or a CI run for `main`/`develop`,
  must never reach the build-and-smoke-test/publish jobs.
- AC-2. The publish job has `permissions: id-token: write` and no
  `environment:` key, and does not reference `secrets.PYPI_API_TOKEN` or
  any other stored PyPI credential anywhere in the file.
- AC-3. The publish job only runs after the build-and-smoke-test job
  succeeds, in the same workflow run for the same tag (`needs:`) - a
  failed smoke test must block the publish step from running at all, not
  just fail loudly alongside it.
- AC-4. The build-and-smoke-test job installs the built **wheel**
  artifact specifically (not `pip install -e .`, not the sdist) into an
  isolated environment before running the smoke command - this is the one
  job that has to prove the actual thing about to reach PyPI works.
- AC-5. A step before the publish job compares the git tag's version
  (stripped of a leading `v`) against `pyproject.toml`'s `version` field
  and fails the workflow on a mismatch, before any publish attempt.

## Error cases

- EC-1. Given the smoke-test command fails (the built wheel is broken),
  the workflow run fails at Job 1 and Job 2 never starts - no publish
  attempt, no partial state on PyPI (nothing was uploaded).
- EC-2. Given the tag version and `pyproject.toml`'s version disagree, the
  workflow fails at the version-guard step (AC-5), before Job 2's
  `uv publish` call - never reaches PyPI with a mismatched/accidental
  version.
- EC-3. Given PyPI's OIDC trusted-publisher config doesn't match this
  workflow (wrong filename, wrong repo) - `uv publish` fails with PyPI
  rejecting the OIDC token exchange. Nothing this workflow can detect in
  advance; documented as an operational precondition (already satisfied
  per Context), not a code path to handle.
- EC-4. Given CI fails for a tag push (a broken commit got tagged), the
  `workflow_run` event still fires (`types: [completed]` covers any
  conclusion) but `conclusion == 'success'` is false - Job 1's `if:` never
  passes, nothing in `cd.yaml` runs at all.
- EC-5. Given CI runs for an ordinary `main`/`develop` push or a PR (not a
  tag), `workflow_run` still fires for those too - `head_branch` won't
  start with `v` (PR) or is literally `main`/`develop` (branch push), so
  Job 1's `if:` filters them out.

## Contracts

- File: `.github/workflows/cd.yaml`
- Trigger: `on: workflow_run: workflows: ["CI"], types: [completed]`,
  gated by `if:` on `github.event.workflow_run.conclusion == 'success'`,
  `.event == 'push'`, and `startsWith(.head_branch, 'v')`.
- Companion change: `.github/workflows/ci.yaml`'s `on.push` gains
  `tags: ["v*.*.*"]` alongside its existing `branches`.
- Job 1: `build-and-smoke-test` - checks out
  `github.event.workflow_run.head_sha` explicitly (workflow_run defaults
  to the default branch otherwise), `uv build`, `pip install` the wheel
  into a throwaway venv, run a smoke command (`ctx --help`, chosen at
  implementation time).
- Job 2: `publish` - `needs: build-and-smoke-test`,
  `permissions: id-token: write`, `uv publish`.
- No new secrets added to the repository.

## Non-goals / open questions

- Exact smoke-test command - left for implementation, any reasonable
  choice satisfies AC-4 as written.
- Whether to also attach the built wheel/sdist to a GitHub Release object
  (separate from the PyPI publish) - not decided, not required by this
  spec's ACs.
- A TestPyPI rehearsal before the first real tag - recommended
  operationally, not automated here (see Scope/Out).
