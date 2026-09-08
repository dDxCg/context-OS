# 011 — CLI prints real output; `ctx diff` uses git revs

Status: implemented

## Context

`app/cli/app.py`'s commands call the right service functions but discard
every return value — `ctx source list`/`history`/`diff` print nothing.
Worse than cosmetic: `sources()` calls `get_sources()` with zero arguments,
but `audit.get_sources(db_handler, ...)` requires `db_handler` — a real
`TypeError` on every invocation, not just silence. `ctx diff --from/--to`
are typed `int | None`, passed straight into `audit.check_diff`, which
forwards them to `git_store.diff` → `subprocess.run([..., v1, v2, ...])` —
`subprocess.run` rejects a non-str/PathLike argument, so a real `int` value
crashes with `TypeError` there too. Both were unreachable via any existing
test (no CLI test file exists), so neither surfaced until now.

## Scope

**In**
- `ctx source list` — constructs a `DBHandler`, prints each source
  (location, provider, status, version).
- `ctx source add`/`remove` — prints a confirmation line per path.
- `ctx history <path>` — prints each version (rev, timestamp, author,
  message), newest first; prints a clear message and exits non-zero on
  `OutOfScopeError`.
- `ctx diff <path> [--from REV --to REV]` — `--from`/`--to` become `str`
  (git revs), not `int`. Omitting both diffs the two most recent versions
  from `get_version_list`. Passing only one of the two is rejected
  (ambiguous). Fewer than 2 versions with both omitted is rejected (nothing
  to diff against).
- `ctx rollback` — since `rollback_source` is still an unimplemented stub
  (deliberately out of scope, tracked separately), the command now says so
  and exits non-zero, instead of silently "succeeding" by calling a no-op.

**Out**
- Implementing `rollback_source` itself (separate spec).
- `ctx health` (`health_check()` is a different, pre-existing stub, not
  part of this spec's scope).

## Acceptance criteria

- AC-1. `ctx source list` prints one line per tracked location, including
  its current version (or a placeholder when there's no git history yet).
- AC-2. `ctx source add <path>` and `ctx source remove <path>` each print a
  confirmation line naming the path.
- AC-3. `ctx history <path>` prints one line per version, newest first,
  including at least the rev and the commit message.
- AC-4. `ctx diff <path>` with no `--from`/`--to` diffs the two most recent
  versions and prints the unified diff text.
- AC-5. `ctx diff <path> --from REV --to REV` diffs exactly those two revs.

## Error cases

- EC-1. `ctx history` on an out-of-scope path prints a message to stderr
  and exits non-zero (does not raise an uncaught `OutOfScopeError`).
- EC-2. `ctx diff` with only one of `--from`/`--to` given prints a message
  to stderr and exits non-zero.
- EC-3. `ctx diff` with neither given, on a path with fewer than 2 versions
  of history, prints a message to stderr and exits non-zero.
- EC-4. `ctx rollback` prints a "not implemented" message to stderr and
  exits non-zero.

## Contracts

No changes to `vcs/services/*` — this spec only rewires
`app/cli/app.py`'s command bodies around the existing `audit.py`/
`configure.py` functions (spec 009).

## Non-goals / open questions

- None outstanding.
