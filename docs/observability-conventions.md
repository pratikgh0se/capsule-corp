---
contract_version: 1
title: Capsule Corp observability conventions
decided: rows 55-66 (2026-09-07), study section 3.3
---

# Capsule Corp observability conventions

Normative for every application and every Capsule Corp component. "Must" and
"never" are requirements; nothing here is a suggestion.

## 1. Resource attributes

Every signal from every process must carry:

- `service.name` — the repo name, exactly (`my-app`, `capsule-corp`,
  `other-app`).
- `service.version` — `git describe` or the `VERSION` constant.
- `deployment.environment.name` — `local`.
- `service.instance.id` — host + pid.

## 2. Metric names and units

- Metric names must use OTel semconv where one exists (`http.server.request.duration`).
- Otherwise metric names must follow `<service>.<noun>.<verb-ed>`, e.g.
  `my_app.session.recorded`, `agent_ops.epic.lane.duration`,
  `sample_mcp.quota.units.consumed`.
- Units belong in the instrument, never in the name (`s`, `By`, `{request}`).

## 3. Attributes

- Attribute sets must be bounded, closed vocabularies.
- `http.route` must be the template, never the raw path.
- `error.type` must come from the closed vocabulary this contract lists.
- Forbidden as a metric attribute, always: user id, task id, todo text,
  video id, raw path.
- **carve-out, service `capsule` only:** a Capsule card id (`task_id`) is an
  allowed label on `samples` rows whose `service` is `capsule`. The forbidden
  list above is about *application* attributes, where a task id is user content
  of unbounded cardinality. Capsule's own card ids are internal, are already a
  first-class column on every `events` row and a path segment in
  `Ledger/<board>/<task_id>.jsonl`, and the probe caps them at the 200 most
  recent runs per board. This is a permissive, additive amendment, so
  `contract_version` stays `1`: section 9 promises that *names* do not change,
  and the quality gate requires an application contract's `contract_version` to
  *equal* this file's, so a bump here would fail every application contract.
  Decided by row 121 (2026-09-09).

## 4. Log line schema

- Log output must be one JSON object per line, one line per request.
- Fields: `ts` (RFC3339), `level`, `service`, `version`, `event`, `route`,
  `status`, `duration_ms`, `request_id`, plus a flat `error` field when
  `level` is WARNING or higher.
- Nothing nested, nothing multi-line — Fluent Bit tails raw lines and Loki
  queries them.

## 5. Log volume

- Log at WARNING and above, plus deliberate lines, matching the existing
  runbook rule.
- One line per request at INFO is deliberate and allowed.
- A debug log inside a loop is never allowed.

## 6. Dashboard layout

Every application dashboard must use this row order:

- Row 0: text panel stating the question the dashboard answers.
- Row 1: **Is it up?** (health, version, uptime).
- Row 2: **RED** (rate, errors, p50/p95/p99 duration).
- Row 3: **Dependencies (USE)**.
- Row 4: **SLO** (compliance gauge over the window + budget remaining).
- Row 5: **Logs** (the Loki panel, filtered to the service).

A links bar must point to the app's other dashboards and back to Mission
Control. Each project must provision its own Grafana folder, named for the
repo.

## 7. Alert rules

Every alert rule must set:

- `labels.severity`: `page` or `notice`.
- `annotations.summary`: what is broken, in user terms.
- `annotations.runbook`: a link to `docs/observability.md#<anchor>` for an
  app that has one; Capsule's own rules (`observability/grafana/provisioning/
  alerting/rules.yaml`) link `docs/RUNBOOK.md#<anchor>` instead, since Capsule
  has no `docs/observability.md`.
- `for:` must never be `0m` on a `page` rule, unless the rule is a dead man's
  switch.

## 8. Capsule `samples` naming

Capsule component metrics (written by the probe into the Postgres `samples`
table, service `capsule`) must follow `capsule.<component>.<noun>[.<unit>]`
— the `<service>.<noun>.<verb-ed>` shape from section 2 relaxed for gauges.
Every label value must come from a closed set (container names, board slugs,
job labels, statuses); never free text.

- **carve-out, `capsule.sandbox.network.attached`:** its `container` label is the
  ephemeral, generated-name sandbox container, not a fixed set. It is bounded by the
  running set (`capsule.sandbox.containers`), so cardinality cannot grow without
  limit. Same shape as the section 3 `task_id` carve-out: permissive and additive,
  `contract_version` stays `1`.

The ten collectors and their sample names, verbatim:

- **postgres**: `capsule.postgres.connections` {state}; `capsule.postgres.db.bytes`;
  `capsule.postgres.table.rows` {table} and `capsule.postgres.table.bytes`
  {table} for `events`, `samples`; `capsule.postgres.events.per_hour` (count
  of events in the last hour); `capsule.postgres.wal.bytes` (pg_current_wal_lsn
  distance from the last tick is out; use `pg_ls_waldir()` sum).
- **boards**: `capsule.board.cards` {board, status}; `capsule.board.runs.24h`
  {board, outcome} from `task_runs`; `capsule.board.run.duration_s.p50` and
  `capsule.board.run.duration_s.p95` {board} over the last 24 h;
  `capsule.board.failures.open` {board}
  (`consecutive_failures > 0`); `capsule.board.db.bytes` {board} and
  `capsule.board.wal.bytes` {board}.
- **docker**: `capsule.container.cpu.pct` {container}; `capsule.container.mem.bytes`
  {container}; `capsule.container.restarts` {container}; `capsule.container.up`
  {container} 1/0.
- **sandbox**: `capsule.sandbox.containers` (count of running containers whose
  image matches `board_bridge.WORKER_IMAGE`'s repository, tag/digest ignored);
  `capsule.sandbox.network.attached` {container} 1/0 - 0 only when `docker
  inspect`'s `HostConfig.NetworkMode` reads `none`; an uninspectable container
  reports 1 (unknown is not "safe"). What this proves and does not prove about
  egress is docs/EGRESS.md's "Proving zero egress" section.
- **launchd**: `capsule.job.loaded` {label} 1/0; `capsule.job.last_exit` {job};
  `capsule.job.last_duration_s` {job}; `capsule.job.age_s` {job} (seconds since
  the stamp).
- **bridge**: `capsule.bridge.tick.age_s`; `capsule.bridge.tick.exit`;
  `capsule.bridge.ledger.pushed` (parsed from the log's `ledger pushed: N
  entries` line).
- **heal**: `capsule.heal.fails` {component}.
- **host**: `capsule.host.disk.pct`; `capsule.host.load1`.
- **cards** (per board, capped to the 200 most recent runs per board):
  `capsule.card.runs`, `capsule.card.duration_s`, `capsule.card.review_rounds`,
  `capsule.card.errors`, `capsule.card.error_repeats` - all {board, task_id}.
  The earlier prototype's session family (`capsule.card.tokens.*`, `turns`, `api_calls`,
  `tool_calls`, `cost_usd`, `runs.unjoined`) is retired (D198/D202): nothing
  produces it.
- **power**: `capsule.delegation.rulings.7d`; `capsule.delegation.decisions.7d`;
  `capsule.card.retrieval.tokens` {board,
  task_id} (row 201/A1 - replayed every tick from board_bridge's
  `retrieval_push` event, the estimate for the vault-chunk block a dev/qa
  card carried at dispatch. No digest line reads it now).

## 9. What version 1 promises

Names in this file must not change without a version bump. A bump is a
decision card for the owner (D-10).

## 10. Factory envelope

Added by OBS-09 (M011), accepted by the owner 2026-10-23. Additive: no v1 name changes, so
`contract_version` stays `1` (section 9). Applies to every Capsule Corp event, log line and
span. Implemented by `capsule/obs_envelope.py` (`emit()`); checked by
`capsule/tests/test_obs_envelope.py`. One flat JSON object per line, no nesting.

- **Resource:** the four section 1 attributes (`service.name`, `service.version`,
  `service.instance.id`, `deployment.environment.name`) plus `host.name`. Nothing assumes one
  host: `service.instance.id` becomes the pod name on Kubernetes.
- **Record:** `ts` (RFC3339 UTC), `severity_text` (`INFO`, `WARN`, `ERROR`), `event.name` (a
  kind from `capsule/event_kinds.py`, vocabulary unchanged), `body` (one line, scrubbed).
- **Correlation:** `capsule.board`, `capsule.task_id`, `capsule.run_id` (the run uuid),
  `trace_id` (32 hex, the fixed hash of `run_id`, W3C format) and `span_id` (16 hex, per step).
  The worker inherits `TRACEPARENT` through its environment (slice 3).
- **Naming:** the OpenTelemetry name wherever one exists (`error.type`, `host.name`);
  otherwise the `capsule.` prefix, dotted and lowercase.
- **Not here:** no OTel SDK, collector or Tempo in the factory (stdlib-only rule); the lines
  are OTel-shaped so a collector file reader can take them later. Loki labels are unchanged:
  ids travel inside the line.
- **Build 2 default (Q-scrape):** scrape only addresses listed in the app index.
- **In Postgres (slice 3, migration 006):** `events` gains nullable `event_name`, `run_id`,
  `trace_id`, `span_id`, written by `capsule/lookout_ship.py` from the envelope. Card events
  ship `event_name` as their payload `event`; other `events.kind` values map through
  `obs_envelope.KIND_NAMES`. A record `problems()` rejects ships with these four null and one
  stderr line. Existing columns and readers are unchanged.
