# 007 — Actor identity on git commits

## Context

Every commit `versioning.py` makes today uses one hardcoded literal,
`DEFAULT_AUTHOR = "unknown:filesystem <unknown@chrono-ctx.local>"` (added in
004 as a placeholder). This spec replaces that with per-event actor
resolution, per the identity model already designed in the narrative draft
this supersedes (`docs/specs/actor-attribution.md`).

**Scope cut from the original draft:** that draft covered MCP-call and
CLI-command identity capture too (`agent:<session-id>`, `cli:<user>`). Both
are unbuildable right now — the MCP tools are still stubs (README: "chưa
nối vào versioning thật") and the CLI barely constructs events at all
(cli-plan.md). Building actor capture for call sites that don't exist yet
would be speculative. This spec builds the **mechanism** (the `actor` field,
its round-trip, and how a handler turns it into a git author + a message
clause) using whatever `event.actor` already carries — `None` for every real
caller today, which must keep behaving exactly as it does now. MCP/CLI
capture become trivial follow-ups once those layers exist: set
`event.actor` before publishing, mechanism does the rest.

## Scope

In scope: `actor: str | None = None` field on `SourceEvent` (and therefore
every subtype); round-trip through `event_to_dict`/`event_from_dict`; a
`_resolve_actor(event)` helper in `versioning.py` producing both a plain
label (for commit messages) and a git-author-formatted string (for
`git_store` calls); all four handlers (`created`/`modified`/`deleted`/
`moved`) use it instead of the `DEFAULT_AUTHOR` literal; `modified_handle`'s
delegation to `created_handle` (when no context exists yet) propagates the
original event's `actor` instead of dropping it.

Out of scope: `guardrail.py`/MCP tool handlers setting `actor` from a
`Context` object; `ctx` CLI commands setting `actor` from `os.getlogin()`/
`CTX_ACTOR`; `config_consumer.py`'s synthetic `CreatedEvent(src=f)` threading
an approving actor through. All three are real follow-ups, blocked on
call sites that don't exist yet, not on anything this spec builds.

## Acceptance criteria

AC-1. Given a `CreatedEvent` constructed with no `actor` argument (every
real caller today), when serialized via `event_to_dict` and rebuilt via
`event_from_dict`, then the result's `actor` is `None` — and a dict from
*before* this field existed (no `"actor"` key at all) still rebuilds without
`KeyError`.

AC-2. Given a `CreatedEvent(src=..., actor="agent:sess-9f3a")`, when
round-tripped through `event_to_dict`/`event_from_dict`, then `actor` comes
back exactly `"agent:sess-9f3a"`.

AC-3. Given `_resolve_actor(event)` where `event.actor == "agent:sess-9f3a"`,
when called, it returns `("agent:sess-9f3a", "agent:sess-9f3a
<agent@chrono-ctx.local>")` — label and git-author-formatted string, name
component taken from the part before the first `:`.

AC-4. Given `_resolve_actor(event)` where `event.actor is None` (today's
default), when called, it returns `("unknown:filesystem",
"unknown:filesystem <unknown@chrono-ctx.local>")` — byte-for-byte what
`DEFAULT_AUTHOR` produces today, so every existing caller's commits are
unaffected.

AC-5. Given `created_handle` is called for an event with
`actor="cli:jane"`, when it commits, then `git log`'s author field is
`"cli:jane <cli@chrono-ctx.local>"` and the commit message ends with
`"via cli:jane"`.

AC-6. Given `modified_handle` is called for a `ModifiedEvent` with
`actor="cli:jane"` on a path with no tracked context yet (the
delegates-to-`created_handle` branch), when it commits, then the resulting
commit's author is `"cli:jane <cli@chrono-ctx.local>"` — not the default —
confirming the actor survives the delegation instead of being dropped.

## Error cases

None beyond AC-1's backward-compatibility case (already an AC, not split
out — there's no new failure mode here, only a field that's optional
everywhere it appears).

## Contracts

```python
# src/vcs/shared/types.py
@dataclass
class SourceEvent:
    src: str
    type: str
    provider: str = "local"
    is_dir: bool = False
    actor: str | None = None   # new
```

```python
# src/vcs/services/versioning.py
def _resolve_actor(event) -> tuple[str, str]:
    """(label, git-author-string) for event.actor, defaulting to the
    filesystem-origin label when unset."""
    actor = getattr(event, "actor", None) or "unknown:filesystem"
    name, _, _ = actor.partition(":")
    return actor, f"{actor} <{name}@chrono-ctx.local>"
```

- `event_to_dict`/`event_from_dict` ([types.py](../../src/vcs/shared/types.py))
  add `"actor"` alongside the existing `"is_dir"` handling — same
  `.get(..., default)` pattern on the decode side for backward compatibility
  with dicts serialized before this field existed.
- Commit messages gain a `via {actor_label}` clause:
  `f"created {relpath} via {actor_label}"`, etc., for all four handlers.
- `DEFAULT_AUTHOR` constant is removed — `_resolve_actor` is now the single
  source of the fallback, not a separately-maintained literal.

## Non-goals / open questions

- MCP/CLI/config-consumer actor capture — real follow-ups, blocked on those
  call sites existing, not by anything here.
- A watched directory edited entirely by hand still produces 100%
  `unknown:filesystem` commits — correct, not a gap this spec tries to close
  (per the superseded draft's "Known gap" reasoning: no reliable way to
  attribute a bare filesystem edit to a real identity, and inferring one
  from file ownership would be actively misleading on a shared machine).
