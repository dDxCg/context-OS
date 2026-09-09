# Draft — audit & tracing log (local, then centralized)

Status: **draft, not started.** Scopes a real, currently-unlogged gap found
by grepping the app layer while writing this draft — not a hypothetical.

## Design goal (confirmed with the user, not assumed)

The point of this feature is **incident response**: when something bad
happens — a file corrupted, content deleted, scope quietly widened — an
operator needs to reconstruct *fast* what happened, when, and who (or what
agent) caused it. Two decisions this shapes, both confirmed directly:

- **Format targets ELK from Phase 1, not as a later conversion.** "Log
  chuẩn trước, hướng đến ELK" — every event is written in an
  [Elastic Common Schema](https://www.elastic.co/guide/en/ecs/current/index.html)-aligned
  shape from day one (see field mapping below), so pointing Filebeat/
  Logstash at chrono-ctx's export later is a shipping config, not a
  redesign of the event shape itself.
- **No new gate for humans.** A human editing a watched file directly
  (outside MCP/CLI) stays ungated, exactly like today — this plan does not
  add authorization checks to plain filesystem access. What it adds is a
  **joined incident timeline**: `audit_events` (agent scope
  grants/denials, elicitation outcomes, CLI/HTTP actions) merged with git
  commit history (every content change, human or agent, already actor-
  attributed per spec 007/013) into one chronological view. Git already
  *sees* human edits (`unknown:filesystem` fallback); this plan's job is
  making that queryable *together* with the agent-side gate decisions that
  have no other record today, not inventing a new human-side capture path.

## The gap, confirmed by reading the code, not assumed

```
grep -rn "logger\.\|logging\.\|get_logger\|log_enabled" src/app/ \
  src/vcs/services/audit.py src/vcs/services/actor_hints.py
```

returns **nothing**. Zero logging exists in the MCP server, the CLI, the
HTTP API, `audit.py`, or `actor_hints.py`. What exists today:

- **Git commit history** (`ctx history`/`ctx diff`, `audit.py`) — a real
  audit trail, but only for *successful* content writes, with actor
  attribution (spec 007/013). A **denied** scope request, a **declined**
  elicitation, or a rejected HTTP API key leaves zero trace anywhere — not
  in git (nothing was written), not in any log (nothing logs it).
- **`data/ctx.log`** (`utils/logger.py`'s `@log_enabled`, used only in
  `vcs/services/` minus `audit.py`, and `vcs/workers/`) — unstructured
  `[SUCCEEDED]`/`[FAILED] funcname` DEBUG text, daemon-side only, no
  security semantics, no actor field, not queryable, not shippable
  anywhere.

Distinguish the two things this plan's title names, since they solve
different problems:

- **Audit**: a discrete, queryable record of security/operational events —
  who did or *tried* to do what, when, allowed or denied. Needed for the
  technical team's stated responsibility in
  [install-integration-plan.md](install-integration-plan.md) ("non-tech
  users never audit — that stays with the technical team").
  Right now they'd have nothing to look at for a denial.
- **Tracing**: correlating *one logical operation* across this project's
  three separate OS processes (MCP server → `pending_actor_hints` handoff →
  daemon's watcher → git commit — see [STATE.md](../STATE.md)'s actor
  attribution section). Today that link is inferred only by actor-label and
  timing proximity, not a real id — exactly the kind of ambiguity this
  session's own live-testing ran into reconstructing which commit came from
  which MCP call (see
  [live-integration-test-plan.md](live-integration-test-plan.md)'s Run log).

## Phase 1 — local audit log (buildable now, no external dependency)

### Storage: dual-write, one queryable, one shippable

Every event is written to **two** places from the same `record_event()`
call — not sequenced phases, both from Phase 1:

1. **`audit_events` table** (`schema.sql`) — what `ctx audit`/`GET
   /v1/audit` query. Practical flat columns, not dotted ECS names (SQLite
   doesn't need that): `id`, `ts`, `actor`, `event_category`, `event_action`,
   `event_outcome`, `resource`, `trace_id`, `detail` (JSON).
2. **`data/audit.jsonl`** (new, append-only, one JSON object per line) —
   the ECS-aligned export a log shipper (Filebeat/Logstash) tails directly.
   This *is* "log chuẩn trước hướng đến ELK": the shape below, not a format
   invented now and converted later.

ECS field mapping (directional alignment on the fields that matter for
incident tracing — not a claim of full ECS compliance, see "Not done
here"):

| chrono-ctx concept | ECS field | Example |
| --- | --- | --- |
| event time | `@timestamp` | `2026-09-10T03:14:07Z` |
| who/what caused it | `user.name` | `agent:s1`, `human:alice`, `unknown:filesystem` |
| coarse kind | `event.category` | `authorization`, `file`, `configuration` |
| specific action | `event.action` | `scope_grant`, `elicitation_decline`, `mcp_write_file`, `cli_rollback_session` |
| allowed/denied/errored | `event.outcome` | `success`, `failure` |
| path touched | `file.path` | `knowledge.example/docs/x.md` |
| cross-process correlation | `trace.id` | matches ECS's own field, not a custom name |

`trace.id` landing on ECS's own standard field (not a made-up name) is
deliberate — it's the one field an ELK-side operator would already expect
to pivot on.

### Recording

One recording function, not scattered `INSERT`s — a small
`vcs/services/audit_log.py` (or fold into `audit.py` — decide when writing
the spec) with `record_event(...)`, writing both targets above from one
call. Called from the few real sites that currently log nothing:
`guardrail.ensure_scope` (grant/deny/elicit outcome), each MCP tool in
`server.py`, `api/deps.require_api_key` (auth failure), the CLI's
`rollback`/`rollback-session` commands. Matches this project's own
convention (STATE.md: business logic in `vcs/services/`, callers stay thin)
rather than inlining log calls at every call site.

`trace_id`: generated once per logical operation (an MCP tool call, an HTTP
request, a CLI command) and threaded through the *same* cross-process
handoff spec 013 already built — either add a `trace_id` column to
`pending_actor_hints` alongside the existing `actor` column, or keep a
small parallel table. The watcher's eventual commit then has the id
available to log against, closing the cross-process correlation gap
without inventing a second handoff mechanism.

### The actual incident-response deliverable: a joined timeline, not just raw event lists

`ctx audit [--since] [--actor] [--event-action]` and `GET /v1/audit`
(HTTP, read-only, same `X-API-Key` gate spec 018 already wired) cover
*browsing* `audit_events` alone — useful, but not the fast-trace goal by
itself, since it's still only half the picture (agent-side gate decisions,
not the actual content history).

The deliverable that actually serves "truy vết nhanh khi có incident" is a
**joined view**: `ctx incident <path> [--since]` (name open to bikeshedding
at spec time) that merges, into one chronological timeline for a given
path:

- every `audit_events` row for that path/trace (scope grants/denials,
  elicitation outcomes) — the *why it was allowed to happen*, and
- every git commit for that path (`git_store.log_history`, already built)
  — the *what actually changed*, author included, whether the author was
  `agent:*` or `unknown:filesystem`.

This is the one command an operator runs mid-incident: "what happened to
this file, in order, who or what did each step." Without it, they'd have
to manually cross-reference `ctx audit` output against `ctx history`
output by eye — exactly the reconstruction-by-hand this feature exists to
remove.

This phase needs its own spec (schema change + a handful of call sites)
once someone is about to build it, per `AGENTS.md`'s rule — not written
here.

## Phase 2 — centralized (design sketch only, deferred pending a driver)

**Not the same gap as [FUTURE.md](../FUTURE.md) item 6** — item 6 is about
distributing *content writes* (multiple edge daemons, one central git
writer), explicitly parked without a concrete driver. This is about
shipping *audit events* somewhere central once more than one machine is
actually running chrono-ctx — a narrower, more concrete question, with a
driver already implied by this project's own
[install-integration-plan.md](install-integration-plan.md): once a
technical team operates more than one helpdesk-managed shared machine,
"audit stays with the technical team" quietly wants one place to look, not
N machines' local `audit_events` tables.

Two shapes, picked only once that's a real, not hypothetical, situation —
both consume Phase 1's `data/audit.jsonl` as-is, since it's already
ECS-shaped:

- **Pull / ship**: point Filebeat (or Logstash) at each machine's
  `data/audit.jsonl` — the boring, standard ELK ingestion path, zero new
  code in this repo at all once Phase 1 exists. The realistic first step
  once there's an actual ELK stack to point at.
- **Push**: reuse the AMQP-shaped routing-key design `LocalEventBus`
  already anticipates (`STATE.md`'s Pub/sub section) — a third routing key
  (`audit.#`, alongside `source.#`/`config.#`) and an `EventBroker` sink,
  the exact extension point item 6 already flags as unbuilt. Only worth it
  over the Filebeat path if near-real-time delivery matters more than
  ELK's own usual polling/tailing latency — shares item 6's own caution:
  don't build the broker side without a real multi-machine deployment to
  justify it.

## Not done here

- Any actual schema or code change — this is a draft, not a spec.
- Full ECS compliance — only the fields that matter for incident tracing
  are mapped (see table above); ECS defines far more than chrono-ctx has
  any data for (`host.*`, `network.*`, etc.) and none of that is invented
  just to look complete.
- The exact shape/name of the joined-timeline command (`ctx incident` is a
  placeholder) — decide at spec time.
- Whether `trace_id` also belongs in git commit messages/trailers (so a
  commit alone reveals its originating operation) or stays a separate
  lookup — open question, decide when writing Phase 1's spec.
- Retention/rotation for `audit_events`/`audit.jsonl` — decide once real
  volume is observed, same stance this project already takes on git-mirror
  GC ([FUTURE.md](../FUTURE.md) item 4).
- Phase 2 itself — sketched only, not to be started without the driver
  described above.
