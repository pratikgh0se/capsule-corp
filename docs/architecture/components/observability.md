# Observability (Lookout feed, probe, Grafana, status screens)

## Purpose
Make the factory inspectable: what is running, what waits on the maintainer, what shipped, what it
cost, what is broken. State comes from the boards (through the Lookout feed) and from host probes;
dashboards and alerts are code.

## Where it lives (files)
- Lookout producer and shipper: `capsule/lookout.py` (tick step 8a reads the board read-only and appends to `$CAPSULE_HOME/lookout/<board>/spool.jsonl`; the tick then kicks `capsule lookout ship` (on unless `lookout.ship` is set false) and an hourly `capsule lookout check` in the background and never waits), `capsule/lookout_ship.py` (spool to ops-pg, with a rejected-rows file and an offset), `capsule/lookout_cli.py` (`capsule lookout ship|status|check|silence`).
- Probe and snapshot: `maintenance/obs_probe.py` (+ `obs_probe_ship.py`, `obs_probe_util.py`, `review_rounds.py`, `probe_boards.py`), `maintenance/live_snapshot.py`, `maintenance/obs_inventory.py`, `maintenance/obs_image_lock.py`, `maintenance/lookout_smoke.py`, `maintenance/lookout_close_stale.py`, `maintenance/db_migrate.py` (numbered SQL under `observability/postgres/migrations/`), `maintenance/events.py`.
- Stack: `observability/docker-compose.yml` (Postgres 16 (database `ops`, called ops-pg in the code), Loki, Fluent Bit, Grafana, Caddy, Infisical with its own Postgres and Valkey, service name `infisical-redis`), `observability/{caddy,fluent-bit,grafana,loki,postgres}/`.
- Dashboards as code: `observability/grafana/build_dashboards.py`, `build_lookout.py`, `build_weekly.py`, `dash_lib.py`, `dash_sql.py`, `lookout_dash_*.py` files (home, system now, phone summary, incidents, merge timeline, delivery, costs, card costs, explorer, trace, alerts), `render_alert_policy.py`.
- Status screens (read-only): `capsule/status_cli.py` and `statusview.py` (`capsule status [--json] [--watch]`), `boardview.py` (`capsule board`), `capsule/epic_graph.py`, `capsule/unblocked.py`, `capsule/obs_envelope.py`.
- Local access: `observability/caddy/Caddyfile` (`cc.<name>.localhost`), maintenance/tailnet_serve.sh (maintainer only, not in the public edition) (Grafana over Tailscale).

## Inputs and outputs
In: board DBs (read-only), run spools, host state (containers, disk, tick processes). Out: rows in ops-pg (`cap_cards`, `cap_runs`, `cap_commits`, `cap_incidents`, tables `samples` (raw 90 d), `samples_hourly` (730 d) and `live_*`, views `v_factory`, `v_floor`, `v_run_cost`, `v_waiting_on_owner`, `v_card_story`, `v_since`), logs in Loki, dashboards, Grafana alerts.

## State it reads and writes
Writes `lookout/spool.jsonl`, its offset and rejected files; ops-pg; Loki. Never writes a board.

## Key rules and invariants
- The board is the source of truth; the Lookout mirrors it. Shipping failures never block the tick (the step never waits, errors are counted as `lookout_errors`).
- Event payloads are allowlisted per kind before they reach the spool (`capsule/event_kinds.py`); logs pass through the scrub filter.
- `capsule status` never writes or ticks; its screen text never prints ids (`--json --refs` does include them).
- Alert rules and contact points are provisioned from `observability/grafana/provisioning/alerting/`. The status screen flags the feed as blind when bytes wait unshipped for over 15 minutes.
- Images are pinned (tags or digests) in `observability/docker-compose.yml`.

## Alert rules
Every rule in `observability/grafana/provisioning/alerting/rules.yaml` links here. Each line gives what fires it and what to check. Page rules: job failed (a vault job exited non-zero in 15 min: read ops-events), bridge sync stopped (no sync tick for 30 min: check the launchd job), Capsule tick stopped (no tick heartbeat for 5 min: check the `capsule tick` process), Loki ingest stopped (no log lines for 30 min: read the fluent-bit and loki container logs, see [observability-compose-stack](observability-compose-stack.md)), backup missing (no good backup for 26 h: run `maintenance/run.sh backup`, see [maintenance-jobs](maintenance-jobs.md)). Notice rules: backup partial (a dump is missing: look for the `*.sql.MISSING` marker in the newest tarball), vault eval failed (read the newest `result-<date>.json`), disk over 85 percent (run the cleanup job), 5-hour window at 85 percent and weekly window at 80 percent (usage meter; hold heavy work, see [dispatch-and-caps](dispatch-and-caps.md)), daily ceiling reached (12 worker runs used; dispatch resumes at midnight), card stuck over an hour (a card waits for the owner), unowned-work count missing (the detector shipped no count for 25 min while the tick is alive; a second firing in 24 h opens a Korin incident), Lookout quarantined spool rows (read `spool.rejected`).

## Failure modes and what happens
Ops-pg or the shipper down: the spool grows, status says the feed is not shipping, alerts are blind until it recovers. A rejected row is quarantined (this page).

## Tests that pin it
`capsule/tests/test_lookout.py`, `test_lookout_ship.py`, `test_lookout_stall.py`, `test_lookout_per_board.py`, `test_status_cli.py`, `test_statusview.py`, `test_status_conformance.py`, `test_boardview.py`, `test_obs_envelope.py`, `test_epic_graph.py`; `maintenance/tests/test_obs_probe.py`, `test_live_snapshot.py`, `test_lookout_dashboards.py`, `test_lookout_smoke.py`, `test_lookout_views.py`, `test_dashboards.py`, `test_build_lookout.py`; shell `tests/test_alert_rules_pg.sh`, `test_deadman_queries_pg.sh`, `test_migration_pg.sh`, `test_front_door.sh`; `contracts/intent/lookout.jsonl`, `lookout_spool_schema.jsonl`, `obs_probe.jsonl`.

## Related decisions and specs
the lookout design note (kept outside the repo), docs/superpowers/specs/2026-09-09-observability-build-1-design.md (maintainer only, not in the public edition), `2026-09-09-observability-build-2-design.md`, `2026-09-06-observability-design.md`; `docs/observability-conventions.md`; rows 476, 478, 480; this page.

## Status
current. App-level monitoring and incident widening for product apps are planned: [../planned.md](../planned.md).
