# Plan — chrono-ctx as an enterprise cowork substrate (tech + non-tech + agent)

Status: **proposed, not started.** This is the synthesis doc: it doesn't
introduce new mechanism, it sequences and cross-links
[competitive-landscape.md](competitive-landscape.md),
[git-backend-plan.md](git-backend-plan.md),
[rabbitmq-migration.md](rabbitmq-migration.md), and
[cli-plan.md](cli-plan.md) against one positioning bet, and flags one real
conflict between two of those docs that needs a decision before Phase 2 below
starts.

## The positioning bet

Every comparable surveyed in [competitive-landscape.md](competitive-landscape.md)
— cachebro, codebase-memory-mcp, knowledge-base-server, palinode, GitAgent —
is a **single-actor, technical-audience** dev tool: one agent or one
developer, locally, versioning context for itself. None target a workspace
where a technical user, a non-technical stakeholder, and an agent read and
write the **same** context sources concurrently, and where the non-technical
party needs to understand and approve what happened without knowing what git
or MCP is.

chrono-ctx already has one piece of infrastructure none of the comparables
have at all: the MCP guardrail's elicitation-based approval
([app/mcp/guardrail.py](../../src/app/mcp/guardrail.py)) — ask a human before
an agent's scope silently grows. That's the seed of the differentiator. It's
not yet aimed at a non-technical human (elicitation only renders inside an
MCP client), and nothing downstream of it (history, diffs, conflicts) is
legible to someone who isn't reading raw file content or git output. Closing
that gap is the plan.

## What "cowork enterprise" concretely requires

Established in prior design discussion, not yet written up until now:

1. **Actor attribution on every version.** Not just *what* changed, *who* —
   human username, agent session id, which MCP approval authorized it. Maps
   directly onto git commit author/email once [git-backend-plan.md](git-backend-plan.md)
   lands — no new field to invent, just populate the existing git primitive
   with actor identity instead of a generic service account.
2. **An approval channel independent of the MCP client.** Elicitation today
   only reaches a technical MCP client. A non-technical stakeholder needs a
   surface that isn't "install an MCP-capable tool" — at minimum a CLI
   command they can be walked through, more realistically a notification/web
   surface outside this repo's scope but depending on `audit.py` /
   `guardrail.py` exposing the right primitives to be driven from outside a
   single MCP session.
3. **Human-legible conflict handling.** Two actors (human + agent, or two
   agents) touching the same file concurrently must not surface as a raw git
   conflict marker. Needs a translation layer over `git diff`/`git log` output
   before this is usable by anyone non-technical.
4. **Commit messages that tell a story.** Auto-generated from the MCP tool
   call or config diff that produced the change, not a generic "watcher
   commit" — turns `git log` into an audit trail a non-technical reader can
   follow without translation.

None of these are storage-layer mechanism — they're what to *populate* the
git primitives with once [git-backend-plan.md](git-backend-plan.md) exists.
That's why storage comes first in the sequencing below.

## Conflict to resolve before Phase 2

[cli-plan.md](cli-plan.md) §3 specs `audit.py`'s four stubs against the
**current SQL/blob backend** (`_get_context_id_by_location`, `BLOB_DIR`
lookups, `available` flag for missing blobs). [git-backend-plan.md](git-backend-plan.md)
specs the same four functions against **git** (`git log`/`show`/`diff`,
scope re-check, `rollback_batch`). These are two different implementations of
the same public surface, written at different points in this project's
design discussion, and only one should get built.

**Recommendation: skip the SQL-based `audit.py` in cli-plan.md §3 entirely,
go straight to the git-based version.** Concretely this also means:
[issues.md](issues.md) **#17 and #18 do not need SQL-level fixes** — building
`created_handle`'s blob-write path correctly or adding a same-hash guard in
the *old* blob store is throwaway work once git (which gets both of those
properties structurally, per [git-backend-plan.md](git-backend-plan.md)'s
issue table) replaces it. Don't spend time hardening code about to be
deleted. Everything else in cli-plan.md — the daemon, the config-control
commands, the WAL/timeout fix — is independent of which storage backend
`audit.py` ends up calling, and stays as specified there.

## Sequencing

### Phase 1 — Daemon/CLI infra (from [cli-plan.md](cli-plan.md), unchanged)

[issues.md](issues.md) #15 (wheel packaging), #16 (SIGTERM handling), #19
(cwd-relative TMP_DIR), #20 (WAL + busy timeout), plus the background daemon
and config-control commands. None of this depends on the storage-backend
decision — it's needed regardless, and blocks everything below (a CLI/daemon
that can't run reliably makes no downstream phase testable end-to-end).

### Phase 2 — Storage backend swap ([git-backend-plan.md](git-backend-plan.md))

Per-dir mirror repos, least-privilege boundary at the storage layer (not just
an app-level filter), single-writer lock, `audit.py` built once against git.
Includes the GC decision already made: rely on git's own `gc.auto` per commit
(zero extra code), add a manual `ctx gc [source]` escape hatch through the
same writer lock, and explicitly do **not** attempt true content erasure of
revoked-scope history (logical revoke via `is_path_in_scope`, not physical
deletion — see [git-backend-plan.md](git-backend-plan.md) "Not done here").

### Phase 3 — Cowork surface

The four requirements above, in dependency order, each now spec'd under
[spec/](spec/):

1. [spec/actor-attribution.md](spec/actor-attribution.md) — identity model
   (MCP/CLI/filesystem-direct), propagation through the event dataclasses,
   git commit author/message. Also settles what a bare filesystem edit (no
   MCP call at all) gets attributed to (`unknown:filesystem` — an honest
   default, not narrowing away the watcher's broader capture surface, which
   [competitive-landscape.md](competitive-landscape.md) identified as a
   strength to keep).
2. [spec/actor-attribution.md](spec/actor-attribution.md) also covers
   commit-message generation — mechanical once identity threading exists,
   folded into the same spec rather than split out.
3. [spec/audit-read-api.md](spec/audit-read-api.md) — read-only HTTP surface
   over `audit.py`, so something other than an MCP client can drive history
   lookups. Prerequisite for 4.
4. [spec/conflict-ux.md](spec/conflict-ux.md) — turned out more concrete than
   expected: the single-writer-lock constraint in
   [git-backend-plan.md](git-backend-plan.md) means there is structurally
   never a git *merge* conflict, only a lost-update race — an
   optimistic-concurrency check (expected-revision gate on writes), not a
   conflict-marker translation layer.

### Phase 4 — Enterprise distribution (extends [rabbitmq-migration.md](rabbitmq-migration.md))

Central-writer topology, spec'd in
[spec/central-writer-distribution.md](spec/central-writer-distribution.md):
edge servers publish source/config events onto the already-designed topic
exchange instead of writing local git mirrors directly; one consumer per
repo (not one global consumer) applies them, each owning that repo's single
write lock from Phase 2. Reuses `event_to_dict`/`event_from_dict`
([types.py](../../src/vcs/shared/types.py)), extended with `actor` per
Phase 3.1 — that serialization was already built anticipating exactly this
out-of-process boundary. At-least-once delivery needs no extra dedup logic:
a redelivered event's commit is a no-op against unchanged content, for the
same structural reason [git-backend-plan.md](git-backend-plan.md) eliminates
[issues.md](issues.md) #18. This is the "scale / centralize view" direction
from the earlier distributed-versioning comparison; the offline-first (git
federation) and data-residency (metadata-only central sync) directions are
deliberately not sequenced here — pick them up only if a concrete driver
(branch-office autonomy requirement, or a data-residency regulation) makes
the central-writer model's single point of write unacceptable.

## Not done here

- **The conflict-translation UI/UX itself** (Phase 3.4) — this doc sequences
  it after its prerequisites, doesn't design it.
- **Git federation / offline-first** and **data-residency metadata-only
  sync** — both real options (see the distributed-versioning comparison
  earlier in this design thread), deliberately deferred until a concrete
  enterprise driver picks one over the central-writer default.
- **True erasure of revoked content** — carried over as a known limitation
  from [git-backend-plan.md](git-backend-plan.md); revisit only under an
  explicit compliance requirement.
- **Multi-tenant / multi-org isolation beyond per-source least privilege** —
  out of scope until an actual multi-org deployment is a real requirement,
  not a hypothetical one.
