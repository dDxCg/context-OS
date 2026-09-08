# 002 — Optimistic-concurrency write gate

## Context

[001-git-mirror-store.md](001-git-mirror-store.md) requires a single writer
lock per repo (`git-backend-plan.md`'s constraint, now implemented as
`_lock_for`/`write` in `vcs/services/git_store.py`). A consequence not
obvious until spelled out: with one serialized writer and no branches,
**this system can never produce a git merge conflict** — every commit lands
on linear history, one at a time. What *can* still happen is a **lost
update**: caller A reads a file, caller B commits a change to it, then A
writes back an edit computed against what it read — silently discarding B's
change with no record a conflict ever existed. This is an
optimistic-concurrency (compare-and-swap) problem, not a merge problem, and
this spec is that check, layered on top of 001's `write`/`head_rev`.

Supersedes the narrative draft at `docs/agents/spec/conflict-ux.md` — this
is the strict AC/EC version that draft's mechanism became once grounded in
what 001 actually built.

## Scope

In scope: `commit_info` (author + timestamp for whatever `head_rev` already
resolves), `write_with_check` (the compare-and-swap gate), `ConcurrentEditError`
carrying enough structured data for a caller to build its own message.

Out of scope: MCP tool wiring (`app/mcp/server.py` accepting `expected_rev`/
`force` from a tool call — separate spec once this primitive exists), the
human-readable message template itself (a caller/formatting concern, not
`git_store`'s), actor-identity resolution (`agent:sess-…` strings — a later
spec; this one accepts whatever opaque `author` string 001 already accepts
and reads back whatever's in the commit, nothing fancier), and
distinguishing "I expect this path to not exist yet" from "I don't care what
state it's in" — both currently collapse to `expected_rev=None` meaning
*skip the check entirely* (see Non-goals).

## Acceptance criteria

AC-1. Given `expected_rev=None`, when `write_with_check(...)` is called on a
path with existing history, then it commits unconditionally and returns the
new rev — identical to calling `write()` directly (today's behavior,
unchanged as the default).

AC-2. Given a path whose current `head_rev` is `R`, when
`write_with_check(..., expected_rev=R)` is called with different content,
then it commits successfully and returns a new rev.

AC-3. Given a path whose current `head_rev` is `R`, when
`write_with_check(..., expected_rev=<a different, stale rev>)` is called,
then it raises `ConcurrentEditError` and **no commit is made** — `head_rev`
after the call still equals `R`.

AC-4. Given the same stale-`expected_rev` situation as AC-3, when
`write_with_check(..., expected_rev=<stale>, force=True)` is called, then it
commits anyway and returns the new rev.

AC-5. Given a `ConcurrentEditError` raised per AC-3, when its attributes are
inspected, then `.path`, `.expected_rev`, `.current_rev`, `.current_author`,
and `.current_timestamp` all match the actual conflicting commit — enough
for a caller to build a message without re-querying `git_store`.

## Error cases

EC-1. Given `commit_info(repo_path, relpath)` is called for a path with no
commit history, it returns `None` — mirrors `head_rev`'s existing contract
(001 EC-2), not an exception.

EC-2. Given `write_with_check(...)` is called against a `repo_path` never
passed to `init_repo()`, it raises `RepoNotInitializedError` — propagated
from the underlying `write()`/`head_rev()` calls (001's guard), not
swallowed or re-wrapped.

## Contracts

```python
# src/vcs/services/git_store.py (additions)

@dataclass
class CommitInfo:
    rev: str
    author: str
    timestamp: str   # git %aI - strict ISO 8601, author date

class ConcurrentEditError(RuntimeError):
    def __init__(self, path: str, expected_rev: str | None, current_rev: str,
                 current_author: str, current_timestamp: str): ...
    # same four attrs exposed on the instance, plus .path

def commit_info(repo_path: Path, relpath: str) -> CommitInfo | None: ...

def write_with_check(
    repo_path: Path, relpath: str, content: bytes, message: str, author: str,
    expected_rev: str | None = None, force: bool = False,
) -> str: ...
```

- No HTTP/MCP surface — pure additions to `vcs/services/git_store.py`,
  same layering as 001.
- Side effects: identical to `write()` (filesystem + one `git commit`) when
  the check passes or is skipped; **no** side effects when it raises (AC-3
  is explicitly not-a-partial-write — verified by asserting `head_rev`
  unchanged, not just that an exception was raised).

## Non-goals / open questions

- `expected_rev=None` always means *skip the check*, even for a path with
  no history yet. A caller wanting strict create-only semantics ("fail if
  this path already has any history") isn't served by this spec's
  mechanism — would need its own sentinel, not built here.
- The human-readable refusal message (`"Cannot write X: changed by Y since
  you last read it..."`) is not this spec's output. `ConcurrentEditError`
  exposes the data; formatting it is deferred to whichever spec wires this
  into an MCP tool or CLI command.
