# 034 — notice when `ctx` isn't on PATH (packaged install)

Status: implemented

## Context

Live-observed this session: `pip install chrono-ctx` (Windows, `--user`)
prints `WARNING: The script ctx.exe is installed in
'...\Scripts' which is not on PATH.` Same class of problem exists on
Linux/macOS (`~/.local/bin` often missing from a fresh user install's
`PATH`).

`ctx` can't fix its own invisibility on the failure case that matters most
(a fresh shell where `ctx <anything>` isn't found at all - the shell never
even runs Python) - flagged already in
[post-release-hardening-plan.md](../agents/draft/post-release-hardening-plan.md)
Part 2. What it *can* do: the moment `ctx` **does** successfully run once
(invoked by its full path, e.g. copy-pasted from pip's own warning, or
because the user is in a shell where it happens to resolve), check whether
it would *also* be found by a bare `ctx` from a fresh shell, and if not,
print a one-time, non-blocking notice with the exact fix command - never
silently edit `PATH`, the user's registry, or a shell rc file (same
reasoning as spec 033/the git-preflight decision: modifying persistent
shell/OS state is a "show, don't do" action, per this repo's existing
`AGENTS.md`-adjacent convention established across this whole draft).

## Scope

**In**
- `utils/helper.py`: `is_packaged_install() -> bool` - `True` for a real
  (non-editable) install, `False` for a source checkout (`uv sync`/`pip
  install -e .`). Thin public wrapper around the already-existing
  `_is_source_checkout()`/`_EDITABLE_CANDIDATE` signal (spec 029) - no new
  detection logic, just exposes the existing one as a public boolean so
  `app.py` doesn't need to reach into `helper`'s underscored internals.
- `app.py`: the root `@cli.callback()` (`main()`, added by spec 032) gains
  a check, after the eager `--version` option is handled: if
  `is_packaged_install()` is true and `shutil.which("ctx") is None`, print
  a one-line, platform-specific fix command to stderr and continue -
  never blocks or exits.
- Skipped entirely for a source checkout - contributors run via `uv run
  ctx`, which resolves correctly by construction; this check would be
  noise for them every single invocation.

**Out**
- No automatic `PATH`/registry/rc-file modification, not even behind a
  flag - out of scope for this spec (draft's Part 2 explicitly considered
  and rejected auto-editing; a future opt-in flag is a separate spec if
  ever picked up).
- No persistent "already warned, don't warn again" state - re-printing the
  notice on every invocation until the user actually fixes their `PATH` is
  the intended behavior (same as `pip`'s own equivalent warning); adding
  suppression state would be speculative complexity for a one-line notice.

## Acceptance criteria

- AC-1. Given a packaged install and `ctx` not resolvable via `PATH`
  lookup, any successful `ctx` invocation prints a fix command to stderr
  containing the actual running script's directory, without blocking or
  changing the command's own exit code/output.
- AC-2. Given `ctx` *is* already resolvable via `PATH`, no notice is
  printed.
- AC-3. Given a source checkout (contributor/CI), no notice is printed
  regardless of `PATH` state - this check is packaged-install-only.
- AC-4. The fix command differs by platform: `setx PATH "%PATH%;<dir>"`
  (plus a note that a new shell is needed) on Windows; `export
  PATH="$PATH:<dir>"` (plus a note to add it to the shell's rc file for
  persistence) on POSIX.
- AC-5. `ctx --version` does not print the PATH notice - the eager
  `--version` option exits before the callback body (where this check
  lives) runs, unchanged from spec 032.

## Error cases

None - a stderr notice, not a new failure mode; must never raise or change
any command's exit code.

## Contracts

```python
# utils/helper.py
def is_packaged_install() -> bool: ...

# app.py
def _path_not_found_hint(script_dir: Path, platform: str) -> str: ...  # pure, testable
```

## Non-goals / open questions

- Whether to also check this at `ctx daemon start` specifically (the
  moment it matters most, since subsequent `ctx daemon status`/`stop`
  calls need `ctx` to resolve again) vs. every invocation - decided: every
  invocation, via the root callback, since that's the only place
  guaranteed to run before any subcommand and needs no extra wiring; a
  `daemon start`-specific message would be redundant with this.
