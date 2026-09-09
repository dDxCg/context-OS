# Draft — shipping: cài nhanh, start nhanh

Status: draft, not started. Distinct from
[install-integration-plan.md](install-integration-plan.md), which answers
*who* installs what for three audiences (non-tech/technical/agent) and is
mostly done (Phase 1/2). This plan answers a different question: **how many
steps, and how many seconds**, for the technical operator who already knows
they're installing chrono-ctx. Two separate clocks, both real:

1. **Cài nhanh** — time from "I want chrono-ctx" to "the daemon is
   installed and configured."
2. **Start nhanh** — time from `ctx daemon start` to "the daemon is
   actually watching and every tracked file's mirror is current."

## Part 1 — cài nhanh (install)

**Today's path** (README Quick start, [runbook-shared-install.md](../../runbook-shared-install.md)):

```bash
git clone https://github.com/dDxCg/chrono-ctx.git && cd chrono-ctx
uv sync
cp config.example.yaml config.yaml   # then hand-edit it
```

Concrete friction, in order of how much it costs:

- **No published package.** `pyproject.toml` already declares
  `[project.scripts] ctx = "app.cli.app:cli"` and the wheel build is
  correct (spec 014 fixed the missing `src/utils` package). But nothing is
  ever built or published from it — installing chrono-ctx today means
  cloning the *entire source repo* (tests, docs, `.git` history) just to
  run one CLI. `pipx install chrono-ctx` / `uvx chrono-ctx` (or even just
  `pip install chrono-ctx` from a built wheel) isn't possible yet - there's
  no artifact to point either at. This is the single biggest lever: it
  turns "clone a git repo, sync a dev environment" into "install a
  package."
- **`git` as a hard runtime dependency**, not just a build-time one - the
  storage backend shells out to the real `git` binary (`STATE.md`'s own
  documented tradeoff, deliberate, not revisited here). A packaged install
  doesn't remove this requirement; it just means the install docs need to
  say "and also have `git` on PATH" regardless of how chrono-ctx itself
  ships. Worth stating plainly in whatever install doc ships alongside a
  published package, not silently assumed like today's README does.
- **Manual `config.yaml` authoring.** `cp config.example.yaml config.yaml`
  then hand-editing YAML is the only path to a working config today - no
  guided first-run equivalent. (`ctx source add <path>` exists but assumes
  a repo already running - it edits an already-initialized `config.yaml`,
  not a first-time one.)

  **Not a separate `ctx init` command - folded into `ctx daemon start`
  itself**, gated on `config.yaml` not existing yet:

  ```
  $ ctx daemon start
  No config.yaml found - let's set one up.
  Use least-privilege scope (watch only this folder: <cwd>)? [Y/n]
  ```

  - **Y (default)** - `add_sources([cwd])`, no further prompting, proceed
    straight to spawning the daemon. Fewest possible prompts for the
    common case, matching the "cài nhanh" goal directly - one yes/no, done.
  - **n** - two explicit alternatives, not a free-form path prompt (a
    free-form prompt reopens the YAML-authoring friction this exists to
    remove):
    ```
    Choose what to watch:
      1) This folder (<cwd>)
      2) Everything from the filesystem root (<C:\> / </>)  - broad, not recommended
    Choice [1/2]:
    ```
    Option 1 here reaches the same `add_sources([cwd])` outcome as the
    default Y path, just via an explicit pick instead of a default -
    option 2 is the one genuinely new, broad outcome, and should carry a
    visible warning (or a typed confirmation, not just a number choice -
    a spec-time decision) given how far it is from least-privilege.

  **Placement, and why it can't live in `Initializer`:** `ctx daemon
  start` (`app/cli/daemon.py`'s `_spawn()`) launches the actual daemon as
  a **detached child with `stdin=subprocess.DEVNULL`** - that process has
  no TTY, so it cannot prompt. `Initializer` is also constructed directly
  by the MCP server and the HTTP API, neither of which has an interactive
  terminal either. The only entry point that is both foreground and
  guaranteed interactive is the `ctx daemon start` **Typer command itself**
  (`app/cli/app.py`'s `daemon_start()`), before it calls `daemon.start()`
  - the prompt has to run and finish (writing a real `config.yaml`) before
  the detached child is ever spawned, not inside it.

  Feasibility check: `configure.add_sources()` already accepts a plain
  path list and already dedupes against broader-already-covered entries -
  this becomes a thin interactive wrapper writing through that existing
  function, not new source-list logic. `init_config_file()` (already
  called unconditionally by `Initializer.__init__`, creates an empty file
  if missing) would need to run first so the file exists for
  `add_sources()` to read/rewrite, same as today.

**Not a lever, already fine:** `uv sync` itself is already fast (a lock
file, no dependency resolution surprises) - the cost above is entirely in
"clone a whole source repo" and "hand-edit YAML," not in the dependency
install step itself.

## Part 2 — start nhanh (boot time)

**Today's path**, in the order `Initializer.init()` actually runs it
(`vcs/initialize.py`, most recently reordered by specs 026/027):

```
create_dirs()
init_db()                              # schema, cheap - one-time DDL
git_store.reset_stale_indexes(...)     # spec 027 - subprocess-per-repo
[old-snapshot read, previously_active] # spec 026 - cheap, DB + one parse
store_config_snapshot()
sync_source_status(...)                # disk walk per current source
reconcile_dropped_sources(...)         # spec 026 - subprocess per dropped path
local_processing() per current source  # full backfill rescan
```

**The real scaling risk is the last line, and it was already there before
specs 026/027 - those two just added more boot-time subprocess work on
top of an existing cost that was never looked at directly.**

`local_processing()` → `local_file_processing()` → `_append_context()`
(`local_adapter.py:23-44`, `versioning.py`) calls `git_store.write()`
**unconditionally, for every file under every currently-configured
source, on every single boot** - not just changed files. `write()`
(`git_store.py:186-209`) itself spawns at minimum 3 `git` subprocesses per
call even when the content turns out to be identical to `HEAD` (
`hash-object`, `update-index`, `write-tree`, plus a `rev-parse`/tree
comparison to detect the no-op) before it can decide nothing changed. That
is **O(total tracked files) subprocess spawns, every boot**, regardless of
whether anything actually changed since the last clean shutdown - the
overwhelmingly common case for a routine restart (a deploy, a reboot, `ctx
daemon stop && ctx daemon start`), not the exception.

Spec 027's `reset_stale_indexes()` adds a further O(repo count) pass
(`rev-parse --verify` + `read-tree` per mirror repo, `os.walk` to find
them), and spec 026's `reconcile_dropped_sources()` adds O(dropped paths).
Both are cheap relative to the backfill scan for typical repo/removal
counts, but they stack on top of it, not instead of it - every boot now
does strictly more subprocess work than before this session, in the name
of correctness (both were real gaps, not spurious). Worth being honest
that "start nhanh" and "boot self-heals everything" are in direct tension,
and the backfill rescan was always the dominant term even before these two
additions.

### Sketch of a fix - not decided, needs its own spec

The backfill rescan exists to self-heal a dropped watcher event or an
edit that happened while the daemon was down (`FUTURE.md` item 5's stated
purpose, achieved today as a side effect of `local_processing()`'s
unconditional rewrite). The fix isn't to remove that guarantee - it's to
make the **common case (nothing changed) cheap** instead of paying the
same subprocess cost as an actual change:

- **Cheap pre-filter before ever shelling to git.** `locations` already
  tracks `st_ino`/`st_dev` per file; adding a persisted `mtime`+`size` (or
  a content hash) per location would let the backfill scan skip
  `git_store.write()` entirely for a file whose `(mtime, size)` matches
  what was last recorded - falling through to the real
  hash-and-compare-to-HEAD path only for files that plausibly changed. Cuts
  the common-case boot from O(all files) subprocess spawns to O(actually-
  changed files) - a schema change (`data/schema.sql`), so its own spec
  per `AGENTS.md` §4's schema-change rule.
- **Don't gate "the daemon is watching" on the backfill finishing.** Right
  now `LocalRuntime.run()`'s watcher only starts after `Initializer.init()`
  returns, so a slow backfill delays real-time watching too, not just the
  self-heal. Starting the watcher first and running the backfill as a
  background pass would decouple "watching live edits" (fast) from
  "self-healed against downtime drift" (can take longer) - real
  correctness tradeoff to think through (a live edit landing on a path the
  backfill hasn't reached yet, mid-scan) that a spec needs to resolve
  explicitly, not simply assumed safe.

Both are real design changes to `versioning.py`/`local_adapter.py`/
`local_runtime.py`, not something to decide by sketching in a draft -
flagged here as the two most promising directions, not a commitment to
either.

## Part 3 — decided: publish to PyPI, install via `pip`/`uv`, not `uvx`/`pipx`

Target end-to-end experience: `pip install chrono-ctx` (or
`uv pip install chrono-ctx`) then `ctx daemon start` - nothing else. That's
a **non-editable** install (a real published package, no local checkout),
which today is actively broken - traced, not assumed:

`anchored()` (`utils/helper.py:13,16-26`) computes
`PROJECT_ROOT = Path(__file__).resolve().parents[2]` - three directories
up from wherever `utils/helper.py` physically sits on disk. In today's
only supported shape (source checkout, `uv sync`/`pip install -e .`) that
file lives at `<repo>/src/utils/helper.py`, so `parents[2]` correctly
lands on `<repo>` - every relative path (`DATABASE_URL`, `CONFIG_PATH`,
`GIT_REPO_DIR`, `SCHEMA_PATH`) resolves consistently from there, which is
the entire point of `anchored()` (its own docstring: keep the MCP server
and the daemon - two separate OS processes, different `cwd`s - agreeing on
the same files). A real `pip install chrono-ctx` from PyPI copies
`utils/helper.py` into `site-packages/utils/helper.py` instead, where
`parents[2]` resolves to a meaningless path (something like the venv's
`lib/pythonX.Y/`) - every data path breaks the same way. This is *not*
specific to `uvx`/`pipx` (which just always install non-editable) - a
plain `pip install chrono-ctx` hits the identical bug. **Fixing this is a
prerequisite for the target flow, not optional polish.**

### Sketch of the `anchored()` fix

Resolution order, replacing the unconditional `parents[2]`:

1. **Per-variable env override** - already exists for each individual
   path (`SNAPSHOT_DIR`, `GIT_REPO_DIR`, `CONFIG_PATH`, `DATABASE_URL`,
   `SCHEMA_PATH` env vars already read independently in
   `vcs/shared/config.py`/`utils/helper.py`) - unchanged, still wins if set.
2. **Source-checkout detection, for today's dev flow to keep working
   unmodified** - if a project marker (`pyproject.toml`, or
   `data/schema.sql`) is actually found near `__file__`'s resolved
   location, anchor there exactly like today. Covers `uv sync`/
   `pip install -e .` with zero behavior change - existing tests, CI, and
   every current contributor workflow stays correct.
3. **Packaged-install default, the new case** - when no marker is found
   (a real `site-packages` install), fall back to an OS-appropriate
   per-user data directory (`platformdirs.user_data_dir("chrono-ctx")` -
   `~/.local/share/chrono-ctx` Linux, `~/Library/Application
   Support/chrono-ctx` macOS, `%LOCALAPPDATA%\chrono-ctx` Windows) instead
   of erroring or silently resolving to nonsense. This is what makes
   `pip install chrono-ctx && ctx daemon start` work with zero
   configuration - `config.yaml`, the SQLite DB, and `GIT_REPO_DIR` all
   live under that one stable directory, and every process (CLI, daemon,
   MCP server, HTTP API) computing the *same* function independent of its
   own `cwd` is what preserves the cross-process-agreement property
   `anchored()` exists for - arguably more robust here than today's
   repo-root anchor, since a user-data-dir doesn't depend on where the
   package happened to be installed from at all.

**Open question, not decided here - multi-project collision.** Today's
model scopes naturally per clone (two checkouts, two independent
`data/`/`config.yaml`). A single global user-data-dir does not - two
unrelated `ctx daemon start` invocations on the same machine (two
different projects/folders the user wants watched independently) would
collide on the same DB/config unless something scopes them apart (a
`--project-dir`/`CHRONO_CTX_HOME` override, or namespacing by cwd hash
under the user-data-dir). Needs an explicit answer at spec time, not an
assumption - this is exactly the kind of thing `ctx daemon start`'s
first-run prompt (Part 1) would need to ask about too, once this lands
("watch scope" and "which data directory" become two different first-run
questions, not one).

`platformdirs` would be a new runtime dependency - not currently in
`pyproject.toml` - a real "dependency needs to be added" decision
(`AGENTS.md` §7), flagged, not silently assumed here.

## Part 4 — CI/CD: build and publish to PyPI via `uv`

**Decided: PyPI only, via OIDC trusted publishing - no npm/choco/brew/apt,
no PyPI API token.** Other channels surveyed and set aside for now:

| Channel | Barrier | Why not (for now) |
| --- | --- | --- |
| npm | low (own account) | Wrong ecosystem for a Python source package - would need a compiled single-binary + JS shim first, the exact "single self-contained executable" tradeoff `STATE.md` explicitly defers, not decided |
| choco (Windows) | account + community-feed review | Package would just wrap `pip install` anyway - another format to maintain per release for no structural win |
| brew (own tap) | none (self-hosted tap, no approval) | Lowest barrier of the four, and `depends_on "git"` would structurally close Part 5's gap on macOS - genuinely worth reconsidering later, just not this pass |
| apt/PPA | Launchpad account, Ubuntu-specific | Real Debian archive needs a maintainer-sponsor process, months-long - a PPA is lighter but still a third packaging format and OS-specific |

None rejected as permanently wrong - brew's tap route in particular
solves the `git`-dependency problem for free and costs nothing to set up
- just not in scope for this pass. PyPI alone is where Part 3's target
flow (`pip install chrono-ctx && ctx daemon start`) actually needs to
land.

**PyPI account/setup requirements, checked, not assumed:**

- A free pypi.org account, but **2FA is mandatory** for every account
  now (PyPI stopped allowing password-only accounts) - a hardware key or
  TOTP app needed before any of this, not optional.
- The project name (`chrono-ctx`) needs to be free on PyPI - not checked
  yet, first real step before anything else here.
- **Trusted publishing (OIDC), chosen over an API token**: PyPI supports
  registering a "pending publisher" for a project name **before it's ever
  been published** - link the GitHub repo + workflow filename + environment
  in the PyPI project settings first, then the *first* publish itself goes
  through OIDC, no manual `twine upload`/token-based bootstrap publish
  ever needed. No long-lived secret to store, rotate, or leak in GitHub
  Actions - the workflow's OIDC identity (repo + workflow + optionally a
  GitHub Environment) is the only thing PyPI trusts, scoped tighter than
  a static token would be.

No release/publish workflow exists today - `.github/workflows/ci.yaml`
only tests (matrix of Python 3.10-3.14, `ruff` + `pytest`), nothing builds
or publishes anything. What Part 3's target flow needs, on top of that:

- **Guard the wheel build itself, actively, not just its config.** Spec
  014 fixed `pyproject.toml`'s missing `src/utils` package but explicitly
  left "actually building/installing a wheel in CI" out of scope, pinning
  the packages list instead of building. A CI step that runs `uv build`,
  `pip install`s the resulting wheel into a throwaway venv (**not**
  editable - this is the one test that actually exercises the
  non-editable path Part 3's fix targets), and runs a smoke command
  (`ctx --help`, or a real `ctx daemon start`/`status`/`stop` round-trip
  against the packaged-install default data directory) would catch both
  the next version of issue #15 (a package forgotten in the wheel list)
  and any regression in Part 3's `anchored()` fallback - the only place
  the non-editable code path would actually run before a real user hits
  it.
- **Tag-triggered publish to PyPI via OIDC.** On a version tag
  (`v*.*.*`): `uv build` (wheel + sdist), then `uv publish` running inside
  a GitHub Actions job with `id-token: write` permission - `uv` handles
  the OIDC token exchange against PyPI's trusted-publisher config
  natively, no `PYPI_API_TOKEN` secret anywhere in the repo. Gates on the
  previous bullet's build-and-smoke-test job passing first, same tag, same
  workflow run - a publish must never ship a wheel that wasn't just proven
  to install and run.
- **Version source of truth.** `pyproject.toml`'s `version = "0.1.0"` is
  currently hand-set and unrelated to git tags - a publish workflow needs
  the tag and the package version to agree (bump `pyproject.toml` as part
  of the tag/release process, or derive the version from the tag at build
  time) - a decision needed before the publish step is real, not before
  this draft.

## Part 5 — UX when `git` isn't on PATH

Traced, not assumed: `GitNotAvailableError` (`git_store.py:34,112`) is
raised when `git init` hits `FileNotFoundError`, but **nothing anywhere
catches it.** The worst concrete case is `ctx daemon start` itself:
`_spawn()` launches the daemon as a **detached child** (`stdin=DEVNULL`,
stdout/stderr redirected to `data/ctx.log`, not the terminal) and the CLI
prints `"daemon started, pid XXXX"` **immediately**, without waiting to
see whether the child actually stays up. The child dies almost instantly
on its first `init_repo()` call - the user sees a success message for a
daemon that is already dead, and only `ctx daemon status` afterward
reveals `not running`, with the actual reason buried in a log file they
have to know to check. Every other entry point (MCP tool call, HTTP API,
a direct `ctx history`/`ctx diff`) at least surfaces the exception
directly to the caller, just as a raw, unfriendly traceback rather than an
explained one. This is a first-run-shaped bug, not an edge case - a
`pip install chrono-ctx` user (Part 3's target audience) is *more* likely
to be missing `git` than someone who already `git clone`d the repo to get
this far today.

**Fix, sketched:**

1. **Preflight in `ctx daemon start` itself, before spawning** -
   `shutil.which("git") is None` → print a clear, actionable message and
   exit non-zero, without ever spawning the child (no wasted PID
   file/log entry for a daemon that was never going to survive). Catches
   the overwhelmingly common case - `git` was never installed - at the one
   point that's both foreground and free (no process to clean up after).
2. **`GitNotAvailableError` specifically, wrapped somewhere sane** (likely
   `VCSRuntime.run()`, around `Initializer.init()`) for the rarer
   mid-lifetime case - `git` was on PATH at `ctx daemon start` time but
   got removed/PATH changed before the daemon actually got there, or a
   later `init_repo()` call for a newly-added source hits it after the
   daemon's been running a while. A clear one-line log entry beats today's
   raw traceback, though this case is inherently harder to surface
   synchronously to the operator the way the preflight check can.

### Should the error message offer to install `git`, or just point at it?

Considered and **not recommended: an interactive "install git for you?"
prompt that shells out to a package manager** (`winget`/`choco` on
Windows, `brew` on macOS, `apt`/`dnf`/`pacman`/... on Linux). Real reasons
against, not just caution:

- `git` is not on PyPI - it can't be added as a normal dependency the way
  every other item in `pyproject.toml` is, which is exactly why this
  question exists at all. Auto-installing it means chrono-ctx becomes
  responsible for detecting *which* package manager exists (Linux alone
  has several, with no reliable single answer) and for invoking it
  correctly on each - real, ongoing cross-platform maintenance and CI
  surface, not a one-time script.
- Most of those installs need elevated privileges (`sudo apt install`,
  and `winget`/`choco` often do too depending on config) - a background
  daemon-start path silently requesting elevation to install arbitrary
  software is a real trust boundary to cross, not something to default
  into behind a single `[y/N]`.
- Directly contradicts the tradeoff `STATE.md` already documents as
  deliberate: `git` is a real, accepted runtime prerequisite, "revisit
  only if the project ever ships as a single packaged executable with no
  external binary assumption" - auto-installing `git` for the user edges
  toward exactly that "fully self-contained" promise without actually
  making the tradeoff, and a network-fetched installer mid-`ctx daemon
  start` directly fights the "start nhanh" goal this whole plan exists
  for.

**Recommended instead: expose it as a clear error with a copy-pasteable,
OS-appropriate install command, never auto-run.**

```
git not found on PATH. Install it, then run `ctx daemon start` again:

  Windows:  winget install --id Git.Git -e
  macOS:    brew install git
  Linux:    sudo apt install git   (or your distro's package manager)

  https://git-scm.com/downloads
```

Detecting the OS to print the *relevant* one line (`sys.platform`, already
used elsewhere in this codebase for the same purpose - `daemon.py`'s
POSIX/Windows branches) costs nothing and turns "go read a webpage" into
"copy one line" without chrono-ctx ever executing anything on the user's
behalf. An explicit, later, opt-in flag (something like `ctx daemon start
--install-git`, only running the OS-native package manager when the user
types that exact flag, never as a default prompt) is a reasonable future
escalation *if* this ever turns out to be a real adoption blocker in
practice - not something to build speculatively now.

## Not done here

- No code changed, no spec written - scoping only, per the "spec gets
  written only once someone is about to implement it" rule.
- The `anchored()` redesign itself (Part 3) - this draft only sketches
  the resolution order; the actual multi-project-collision question, the
  `platformdirs` dependency decision, and the implementation are their own
  spec.
- Building the wheel-build-and-smoke-test CI job or the tag-triggered
  PyPI publish workflow (Part 4) - separate spec once someone picks this
  up, including the version-source-of-truth decision.
- Building the `ctx daemon start` git-missing preflight check or the
  `GitNotAvailableError` wrapping (Part 5) - separate spec, sketched only;
  no auto-install script - explicitly rejected above, not deferred.
- Building the `ctx daemon start` first-run prompt (Part 1) - separate
  spec, sketched only; now also needs to account for Part 3's "which data
  directory" question once both land together.
- Measuring actual boot time on a real repo of realistic size (Part 2) -
  everything there is reasoned from reading the code's subprocess-per-file
  shape, not a live timing run.
- Any change to the `git`-as-a-dependency tradeoff - a deliberate,
  already-decided cost (`STATE.md`), not revisited here. A packaged
  `pip install chrono-ctx` still requires `git` on PATH separately -
  worth stating loudly in the PyPI project description/README once
  Part 4 ships, since a packaged install more easily implies "fully
  self-contained" than a source checkout does.
