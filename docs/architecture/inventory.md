# Inventory: every module, script and job mapped to its page

First generated from the tree at `main` @ `c6b4bb50` (2026-10-28); re-verified at `dabd6a8b` (2026-11-12): rows for deleted files dropped, rows for new files added (earlier-prototype leftovers that still exist stay, marked as such). A path marked
`undocumented` could not be placed. Rule: adding, renaming or removing a file here updates this table
in the same change. Pages are under [components/](components/).

Coverage: every listed file ships in the public edition (checked 2026-11-27 against release/public-paths.txt). Maintainer-only files (for example the deploy and bootstrap scripts, the author overlay prompts and vault jobs) are not listed. Files under `capsule/tests/`, `maintenance/tests/` and `tests/` are not listed one by one (each page names the tests that pin it).

## capsule/ modules (`capsule/*.py`, `capsule/models/`, `capsule/runners/`)

| Path | Page | Note |
|---|---|---|
| `capsule/__init__.py` | [tick-scheduler](components/tick-scheduler.md) |  |
| `capsule/__main__.py` | [tick-scheduler](components/tick-scheduler.md) |  |
| `capsule/amend.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) |  |
| `capsule/app_events.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | app failures as incidents (D79, D89) |
| `capsule/assign.py` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `capsule/autoamend.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) |  |
| `capsule/blocks.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) |  |
| `capsule/boardview.py` | [observability](components/observability.md) |  |
| `capsule/boundary.py` | [tick-scheduler](components/tick-scheduler.md) |  |
| `capsule/brainstorm.py` | [brainstorm-runner](components/brainstorm-runner.md) | open and close a Slack brainstorm (M204, D229) |
| `capsule/brainstorm_check.py` | [brainstorm-runner](components/brainstorm-runner.md) |  |
| `capsule/brainstorm_intake.py` | [brainstorm-runner](components/brainstorm-runner.md) |  |
| `capsule/brainstorm_run.py` | [brainstorm-runner](components/brainstorm-runner.md) |  |
| `capsule/brainstorm_store.py` | [brainstorm-runner](components/brainstorm-runner.md) | files of one brainstorm |
| `capsule/bundles_guide.py` | [worker-runner-and-images](components/worker-runner-and-images.md) | guide, Korin and ruling bundles split out of bundles.py |
| `capsule/contract_audit.py` | [gate-review-checks](components/gate-review-checks.md) | soft audit of the card's acceptance rows (D220) |
| `capsule/host.py` | [public-edition](components/public-edition.md) | host.json host-level settings (P05a) |
| `capsule/landing_seen.py` | [lander-and-keeper](components/lander-and-keeper.md) | split out of landing.py |
| `capsule/prompts/brainstorm.md` | [brainstorm-runner](components/brainstorm-runner.md) |  |
| `capsule/bundles.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/card_fields.py` | [board-store](components/board-store.md) |  |
| `capsule/cardmeta.py` | [board-store](components/board-store.md) |  |
| `capsule/check_gate.py` | [gate-review-checks](components/gate-review-checks.md) |  |
| `capsule/checkpoint.py` | [board-store](components/board-store.md) |  |
| `capsule/ci_watch.py` | [lander-and-keeper](components/lander-and-keeper.md) |  |
| `capsule/citations.py` | [board-store](components/board-store.md) |  |
| `capsule/cli_intake.py` | [intake](components/intake.md) |  |
| `capsule/cli_names.py` | [intake](components/intake.md) |  |
| `capsule/cli_ops.py` | [tick-scheduler](components/tick-scheduler.md) |  |
| `capsule/cli_prior.py` | [intake](components/intake.md) |  |
| `capsule/close_routine_nudges.py` | [intake](components/intake.md) |  |
| `capsule/config.py` | [tick-scheduler](components/tick-scheduler.md) |  |
| `capsule/contracts.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/contradiction.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) |  |
| `capsule/cost.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) |  |
| `capsule/db.py` | [board-store](components/board-store.md) |  |
| `capsule/dispatch.py` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `capsule/dispatch_gate.py` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `capsule/docker_door.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/epic_clean.py` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `capsule/epic_graph.py` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `capsule/epic_sync.py` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `capsule/eval_check.py` | [evals-and-report](components/evals-and-report.md) |  |
| `capsule/eval_tokens.py` | [evals-and-report](components/evals-and-report.md) |  |
| `capsule/event_kinds.py` | [board-store](components/board-store.md) |  |
| `capsule/golden.py` | [evals-and-report](components/evals-and-report.md) |  |
| `capsule/golden_cli.py` | [evals-and-report](components/evals-and-report.md) |  |
| `capsule/guide_notes.py` | [role-guide](components/role-guide.md) |  |
| `capsule/harness_gate.py` | [evals-and-report](components/evals-and-report.md) |  |
| `capsule/human.py` | [intake](components/intake.md) |  |
| `capsule/human_minutes.py` | [evals-and-report](components/evals-and-report.md) |  |
| `capsule/identity.py` | [intake](components/intake.md) |  |
| `capsule/idle_lane.py` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `capsule/incident_attach.py` | [incidents-korin-flow](components/incidents-korin-flow.md) |  |
| `capsule/incident_blind_run.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | blind recurrence ids (D161) |
| `capsule/incident_landed.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | `incidents landed` |
| `capsule/incident_args.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | `row`, `landed`, `close` arguments |
| `capsule/incident_inputs.py` | [incidents-korin-flow](components/incidents-korin-flow.md) |  |
| `capsule/incident_recurrence.py` | [incidents-korin-flow](components/incidents-korin-flow.md) |  |
| `capsule/incident_watch.py` | [incidents-korin-flow](components/incidents-korin-flow.md) |  |
| `capsule/incidents.py` | [incidents-korin-flow](components/incidents-korin-flow.md) |  |
| `capsule/incidents_cli.py` | [incidents-korin-flow](components/incidents-korin-flow.md) |  |
| `capsule/intake.py` | [intake](components/intake.md) |  |
| `capsule/intent.py` | [gate-intent-contracts](components/gate-intent-contracts.md) |  |
| `capsule/intent_example_schema.py` | [gate-intent-contracts](components/gate-intent-contracts.md) |  |
| `capsule/interventions.py` | [intake](components/intake.md) |  |
| `capsule/invalid_doc.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) |  |
| `capsule/invariants.py` | [tick-scheduler](components/tick-scheduler.md) |  |
| `capsule/journal.py` | [board-store](components/board-store.md) |  |
| `capsule/jsonschema.py` | [board-store](components/board-store.md) |  |
| `capsule/keeper_watch.py` | [lander-and-keeper](components/lander-and-keeper.md) |  |
| `capsule/lander.py` | [lander-and-keeper](components/lander-and-keeper.md) |  |
| `capsule/landing.py` | [lander-and-keeper](components/lander-and-keeper.md) |  |
| `capsule/lane_behind.py` | [dispatch-and-caps](components/dispatch-and-caps.md) | lane-behind detector (L155) |
| `capsule/lease.py` | [board-store](components/board-store.md) |  |
| `capsule/lookout.py` | [observability](components/observability.md) |  |
| `capsule/lookout_cli.py` | [observability](components/observability.md) |  |
| `capsule/lookout_ship.py` | [observability](components/observability.md) |  |
| `capsule/loophole.py` | [gate-intent-contracts](components/gate-intent-contracts.md) |  |
| `capsule/metrics.py` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `capsule/models/__init__.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/models/claude.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/models/stub.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/obs_envelope.py` | [outbox-notifications](components/outbox-notifications.md) |  |
| `capsule/outbox.py` | [outbox-notifications](components/outbox-notifications.md) |  |
| `capsule/outcomes.py` | [evals-and-report](components/evals-and-report.md) |  |
| `capsule/override.py` | [intake](components/intake.md) |  |
| `capsule/paths.py` | [tick-scheduler](components/tick-scheduler.md) |  |
| `capsule/preflight.py` | [tick-scheduler](components/tick-scheduler.md) |  |
| `capsule/prior_runs.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/prior_verdicts.py` | [intake](components/intake.md) |  |
| `capsule/report.py` | [evals-and-report](components/evals-and-report.md) |  |
| `capsule/report_notes.py` | [evals-and-report](components/evals-and-report.md) |  |
| `capsule/restart.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) |  |
| `capsule/review_findings.py` | [gate-review-checks](components/gate-review-checks.md) |  |
| `capsule/review_scope.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) |  |
| `capsule/routine_nudges.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | interventions that open no incident; split out of incidents.py |
| `capsule/row_schemas.py` | [board-store](components/board-store.md) |  |
| `capsule/ruling_sweep.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) |  |
| `capsule/run_record.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/runaway.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) |  |
| `capsule/runners/__init__.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/runners/docker.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/runners/exec_mcp.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/runners/host.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/runners/mac.py` | [worker-runner-and-images](components/worker-runner-and-images.md) | Mac lane (D305): MacRunner, lane availability reasons, mac-lane.lock |
| `capsule/runners/seatbelt.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/safe_write.py` | [board-store](components/board-store.md) |  |
| `capsule/seats.py` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `capsule/slack.py` | [outbox-notifications](components/outbox-notifications.md) |  |
| `capsule/slack_answer.py` | [outbox-notifications](components/outbox-notifications.md) | Slack
answer process; PROTECTED |
| `capsule/slack_digest.py` | [outbox-notifications](components/outbox-notifications.md) |  |
| `capsule/slack_listen.py` | [outbox-notifications](components/outbox-notifications.md) | Slack Socket Mode listener, intake only (D225) |
| `capsule/slack_msgs.py` | [outbox-notifications](components/outbox-notifications.md) |  |
| `capsule/slack_secret.py` | [outbox-notifications](components/outbox-notifications.md) | token reader and log masking for the listener |
| `capsule/spool.py` | [board-store](components/board-store.md) |  |
| `capsule/patrol.py` | [patrol](components/patrol.md) | facts, agent call, apply |
| `capsule/patrol_check.py` | [patrol](components/patrol.md) | document check |
| `capsule/patrol_facts.py` | [patrol](components/patrol.md) | second look, `why`, work list |
| `capsule/patrol_run.py` | [patrol](components/patrol.md) | `capsule patrol`, `capsule why` |
| `capsule/stall.py` | [stall-supervisor](components/stall-supervisor.md) |  |
| `capsule/stall_cli.py` | [stall-supervisor](components/stall-supervisor.md) | `capsule stalls` |
| `capsule/stall_patrol.py` | [patrol](components/patrol.md) | class `patrol_agent_down` |
| `capsule/status_cli.py` | [observability](components/observability.md) |  |
| `capsule/statusview.py` | [observability](components/observability.md) |  |
| `capsule/stop_queue.py` | [intake](components/intake.md) | verb `stop '<trigger>'` |
| `capsule/style_lint.py` | [gate-review-checks](components/gate-review-checks.md) |  |
| `capsule/tick.py` | [tick-scheduler](components/tick-scheduler.md) |  |
| `capsule/tick_guard.py` | [tick-scheduler](components/tick-scheduler.md) |  |
| `capsule/tick_review.py` | [tick-scheduler](components/tick-scheduler.md) | `TickReview` mixin |
| `capsule/tick_rows.py` | [tick-scheduler](components/tick-scheduler.md) | free functions: decision
rows for the guide |
| `capsule/tick_rulings.py` | [tick-scheduler](components/tick-scheduler.md) | `TickRulings` mixin |
| `capsule/tick_runs.py` | [tick-scheduler](components/tick-scheduler.md) | `TickRuns` mixin |
| `capsule/token_accountant.py` | [evals-and-report](components/evals-and-report.md) |  |
| `capsule/touch.py` | [intake](components/intake.md) |  |
| `capsule/touches.py` | [evals-and-report](components/evals-and-report.md) |  |
| `capsule/unblocked.py` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `capsule/unblocked_stop.py` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `capsule/unowned_idle.py` | [stall-supervisor](components/stall-supervisor.md) | dead-man check |
| `capsule/usage_meter.py` | [dispatch-and-caps](components/dispatch-and-caps.md) | weekly usage meter (D234) |
| `capsule/validator_report.py` | [role-validator](components/role-validator.md) |  |
| `capsule/weekly_pause.py` | [dispatch-and-caps](components/dispatch-and-caps.md) | tick starts no new runs past usage_pause_at (D234) |
| `capsule/worker.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/workspace.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `capsule/answer_base.py` | [outbox-notifications](components/outbox-notifications.md) |
stdlib-only base of the answer path; PROTECTED |
| `capsule/answer_scope.py` | [intake](components/intake.md) | an answer that narrows a card must go through amend |
| `capsule/auditor.py` | [role-auditor](components/role-auditor.md) | audit packet builder |
| `capsule/blind/brief.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/checkout.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/cli.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/close.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/dispatch.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/exempt.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/gate.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/harness/api.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/harness/unittest_runner.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/launch.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/lint.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/preflight.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/red.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/run.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/scan.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/settle.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/snapshot.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/text.py` | [blind-path](components/blind-path.md) |  |
| `capsule/blind/unavailable.py` | [blind-path](components/blind-path.md) |  |
| `capsule/board_tick.py` | [tick-scheduler](components/tick-scheduler.md) | `capsule board tick`: ready a board tick job without deploy.sh |
| `capsule/bundle_sections.py` | [worker-runner-and-images](components/worker-runner-and-images.md) | section builders shared by bundles |
| `capsule/claims.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) | pre-flight claim check |
| `capsule/clone_state.py` | [dispatch-and-caps](components/dispatch-and-caps.md) | what a clone carries uncommitted at dispatch |
| `capsule/contract_jobs.py` | [blind-path](components/blind-path.md) | validator contract commands off the tick thread |
| `capsule/ask_gate.py` | [ask-gate](components/ask-gate.md) | owner-question gate, units 1 to 3 |
| `capsule/ask_apply.py` | [ask-gate](components/ask-gate.md) | apply and tell (unit 3) |
| `capsule/cos_ask.py` | [cos-tools](components/cos-tools.md) |  |
| `capsule/cos_brief.py` | [cos-tools](components/cos-tools.md) |  |
| `capsule/daily_pr.py` | [product-board-landing](components/product-board-landing.md) |  |
| `capsule/day_branch.py` | [product-board-landing](components/product-board-landing.md) |  |
| `capsule/design_briefs.py` | [dispatch-and-caps](components/dispatch-and-caps.md) | a risky card waits for its design brief |
| `capsule/diff_ceiling.py` | [lander-and-keeper](components/lander-and-keeper.md) | real diff over `diff_lines_park` is parked |
| `capsule/docker_argv.py` | [worker-runner-and-images](components/worker-runner-and-images.md) | masks mount sources in a stored docker failure |
| `capsule/docker_down.py` | [detectors-z5](components/detectors-z5.md) |  |
| `capsule/doctor.py` | [public-edition](components/public-edition.md) |  |
| `capsule/exec_outage.py` | [detectors-z5](components/detectors-z5.md) |  |
| `capsule/fix_card_drift.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | a fix card main has overtaken says so |
| `capsule/fix_card_quotes.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | copies named human files into a fix card |
| `capsule/flake.py` | [gate-review-checks](components/gate-review-checks.md) | flaky-test reruns (`redesign.flake_reruns`) |
| `capsule/follow_ups.py` | [intake](components/intake.md) | report follow_ups become intake cards |
| `capsule/golden_swap.py` | [evals-and-report](components/evals-and-report.md) | model swap must pass the golden set |
| `capsule/hollow_tests.py` | [gate-mutation-check](components/gate-mutation-check.md) | warn-only hollow-test detector |
| `capsule/human_preflight.py` | [intake](components/intake.md) | refuses unblock or accept on a parked card early |
| `capsule/image_build.py` | [image-record](components/image-record.md) |  |
| `capsule/image_ledger.py` | [image-record](components/image-record.md) |  |
| `capsule/image_render.py` | [image-record](components/image-record.md) |  |
| `capsule/image_request_run.py` | [image-record](components/image-record.md) |  |
| `capsule/image_requests.py` | [image-record](components/image-record.md) |  |
| `capsule/image_toml.py` | [image-record](components/image-record.md) |  |
| `capsule/incident_blind.py` | [blind-path](components/blind-path.md) | blind-mount incident |
| `capsule/incident_history.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | close pre-Korin incidents as history (row 638) |
| `capsule/incident_leak.py` | [blind-path](components/blind-path.md) | blind leak-lint incident |
| `capsule/incident_match.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | intent pattern language for Korin examples |
| `capsule/install.py` | [public-edition](components/public-edition.md) |  |
| `capsule/lander_blind.py` | [blind-path](components/blind-path.md) | lander re-run of blind tests |
| `capsule/landing_journal.py` | [lander-and-keeper](components/lander-and-keeper.md) |  |
| `capsule/landing_reverts.py` | [lander-and-keeper](components/lander-and-keeper.md) |  |
| `capsule/landing_rows.py` | [lander-and-keeper](components/lander-and-keeper.md) |  |
| `capsule/plan_cut.py` | [intake](components/intake.md) | `capsule plan check|cut` |
| `capsule/plists.py` | [public-edition](components/public-edition.md) |  |
| `capsule/probe_detail.py` | [worker-runner-and-images](components/worker-runner-and-images.md) | what a failed container start leaves for the reader |
| `capsule/protected_scope.py` | [gate-protected-paths](components/gate-protected-paths.md) |  |
| `capsule/publish.py` | [product-board-landing](components/product-board-landing.md) |  |
| `capsule/replay.py` | [dispatch-and-caps](components/dispatch-and-caps.md) | replay harness for dispatch switches |
| `capsule/reviewer_rerun.py` | [role-reviewer](components/role-reviewer.md) |  |
| `capsule/rhythm.py` | [product-board-landing](components/product-board-landing.md) |  |
| `capsule/risk.py` | [dispatch-and-caps](components/dispatch-and-caps.md) | deterministic risk classifier |
| `capsule/runners/host_wall.py` | [blind-path](components/blind-path.md) | host wall |
| `capsule/runners/sandbox.py` | [lander-and-keeper](components/lander-and-keeper.md) | lander suites in the worker image |
| `capsule/secret_guard_ship.py` | [secret-read-guard](components/secret-read-guard.md) | ships the guard log to ops-pg |
| `capsule/security.py` | [role-security-reviewer](components/role-security-reviewer.md) | scan verdicts |
| `capsule/security_review.py` | [role-security-reviewer](components/role-security-reviewer.md) |  |
| `capsule/shards.py` | [role-validator](components/role-validator.md) | contract command sharding |
| `capsule/test_shard.py` | [role-validator](components/role-validator.md) |  |
| `capsule/threat_model.py` | [role-security-reviewer](components/role-security-reviewer.md) | threat-model check |
| `capsule/vault_context.py` | [vault-context](components/vault-context.md) |  |
| `capsule/vault_context_cli.py` | [vault-context](components/vault-context.md) |  |
| `capsule/vault_eval.py` | [vault-context](components/vault-context.md) |  |
| `capsule/wall_clock.py` | [tick-scheduler](components/tick-scheduler.md) | wall-clock cap starts at the real spawn |
| `capsule/engine_heal.py` | [stall-supervisor](components/stall-supervisor.md) | skip rule of a hung-engine hard restart |
| `capsule/ask_panel.py` | [ask-gate](components/ask-gate.md) | panel (unit 2): two blind seats, one agent call each |
| `capsule/ask_log.py` | [ask-gate](components/ask-gate.md) | dossier and queue files and verdict log under `<board>/asks/` |
| `capsule/ask_refs.py` | [ask-gate](components/ask-gate.md) | source readers and word-for-word quote check |
| `capsule/base_red.py` | [blind-path](components/blind-path.md) | re-run of a failed contract command on the card's start commit |
| `capsule/host_guard.py` | [stall-supervisor](components/stall-supervisor.md) | disk rows and failure storm (unit 4b) |
| `capsule/host_registry.py` | [public-edition](components/public-edition.md) | which boards and repos a host serves |
| `capsule/incident_duplicate.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | `incidents duplicate`: mark a repeat as superseded |
| `capsule/jobs_dir.py` | [maintenance-jobs](components/maintenance-jobs.md) | the jobs' runtime config dir |
| `capsule/lander_select.py` | [lander-and-keeper](components/lander-and-keeper.md) | selected-tests mode (D273) |
| `capsule/launch_kick.py` | [tick-scheduler](components/tick-scheduler.md) | live `launchctl kickstart` probe |
| `capsule/legacy_names.py` | [public-edition](components/public-edition.md) | where the retired old system's name may still appear (D270) |
| `capsule/log_caps.py` | [maintenance-jobs](components/maintenance-jobs.md) | log size caps, check only |
| `capsule/main_waits.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) | a commit wait that ends by itself |
| `capsule/stall_continue.py` | [stall-supervisor](components/stall-supervisor.md) | quiet stops: continue is the default |
| `capsule/stall_decided.py` | [stall-supervisor](components/stall-supervisor.md) | a decision Capsule made alone, as a human answer in a worker brief |
| `capsule/stall_idle.py` | [stall-supervisor](components/stall-supervisor.md) | a ready card that nothing explains (`no_progress`) |
| `capsule/stall_owner.py` | [stall-supervisor](components/stall-supervisor.md) | owner-wait timers (D339) |
| `capsule/stall_page.py` | [stall-supervisor](components/stall-supervisor.md) | incident and page helpers of the stall supervisor |
| `capsule/stall_probes.py` | [stall-supervisor](components/stall-supervisor.md) | process and file facts the stall remedies use |
| `capsule/stall_remedies.py` | [stall-supervisor](components/stall-supervisor.md) | unit 3: runs, review and landing detectors and L0 remedies |
| `capsule/stall_ruling.py` | [stall-supervisor](components/stall-supervisor.md) | unit 2: the L1 stall ruling |
| `capsule/stall_silence.py` | [stall-supervisor](components/stall-supervisor.md) | silent-run facts (CPU samples, quiet window) |
| `capsule/stall_veto.py` | [stall-supervisor](components/stall-supervisor.md) | `capsule veto`: take back a decision Capsule made alone |
| `capsule/tick_watch.py` | [stall-supervisor](components/stall-supervisor.md) | tick watchdog (unit 4a): is each tick alive or wedged |
| `capsule/stall_continue_text.py` | [stall-supervisor](components/stall-supervisor.md) | the text a continue sends (split from `stall_continue`) |
| `capsule/missing_module.py` | [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md) | a runner that lacks a Python module is a `capability` hold, not the card's failure |
| `capsule/blind/__init__.py` | [blind-path](components/blind-path.md) | ADDED 2026-11-27 |
| `capsule/board_schema.sql` | [board-store](components/board-store.md) | ADDED 2026-11-27: base SQLite schema |
| `capsule/config.example.json` | [tick-scheduler](components/tick-scheduler.md) | ADDED 2026-11-27: board config example |
| `capsule/config.capsule-corp.example.json` | [tick-scheduler](components/tick-scheduler.md) | ADDED 2026-11-27: board config example |
| `capsule/eval_check.json` | [evals-and-report](components/evals-and-report.md) | ADDED 2026-11-27: eval check data |
| `capsule/incident_repo.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | ADDED 2026-11-27: which repo holds an incident fix |
| `capsule/korin_checkout.py` | [incidents-korin-flow](components/incidents-korin-flow.md) | ADDED 2026-11-27: refuses a Korin run in the wrong checkout |
| `capsule/interrupts.json` | [outbox-notifications](components/outbox-notifications.md) | ADDED 2026-11-27: data read by outbox.py and workspace.py |
| `capsule/licenses/*.LICENSE` | [public-edition](components/public-edition.md) | ADDED 2026-11-27: licence text of vendored third-party code |

## maintenance/ scripts, templates and hooks

| Path | Page | Note |
|---|---|---|
| `maintenance/absence.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/ai_usage.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover. Claude Code scan: recursive over `projects/**/*.jsonl` (subagents included), deduped on message id + requestId (max per field), bucketed by record timestamp |
| `maintenance/backup.sh` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/backup_key.sh` | [secrets](components/secrets.md) |  |
| `maintenance/board.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/board_bridge.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover; imported by `capsule/brainstorm.py` |
| `maintenance/board_errors.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/board_fence.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/board_view.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/brief.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/budgets.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover; imported by `capsule/intake.py` and `capsule/diff_ceiling.py` |
| `maintenance/cleanup.sh` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/contracts.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/dark_test.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/db_migrate.py` | [observability](components/observability.md) |  |
| `maintenance/decision_log.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover; imported by `capsule/touches.py` and `capsule/vault_context.py` |
| `maintenance/decisions.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/deps.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/deps_sync.sh` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/docker_idle.py` | [maintenance-jobs](components/maintenance-jobs.md) | Docker idle reaper (D195, D204) |
| `maintenance/epic_lane.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/events.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/grafana_prune_folders.sh` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/heal.sh` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/heal_table.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/hooks/sprint-plan-guard.py` | [maintenance-jobs](components/maintenance-jobs.md) | Claude PreToolUse write guards for unattended vault jobs |
| `maintenance/hooks/sprint-plan.settings.json` | [maintenance-jobs](components/maintenance-jobs.md) | Claude PreToolUse write guards for unattended vault jobs |
| `maintenance/hooks/write-guard.py` | [maintenance-jobs](components/maintenance-jobs.md) | Claude PreToolUse write guards for unattended vault jobs |
| `maintenance/hooks/write-guard.settings.json` | [maintenance-jobs](components/maintenance-jobs.md) | Claude PreToolUse write guards for unattended vault jobs |
| `maintenance/intake.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/ledger.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/lib_compose.sh` | [compose-live-guard](components/compose-live-guard.md) |  |
| `maintenance/lib_send.sh` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/lib_state.sh` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/live_snapshot.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/log_rotate.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/lookout_close_stale.py` | [observability](components/observability.md) |  |
| `maintenance/lookout_smoke.py` | [observability](components/observability.md) |  |
| `maintenance/merge_gate.py` | [maintenance-jobs](components/maintenance-jobs.md) | helpers only since D238 (spec and release stamps, `run_in_clone`, `test_cmd_for`, state dir); the per-card pipeline is retired |
| `maintenance/messages.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/meters.py` | [evals-and-report](components/evals-and-report.md) |  |
| `maintenance/migration_lint.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/notice_gen.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/obs_image_lock.py` | [observability](components/observability.md) |  |
| `maintenance/obs_inventory.py` | [observability](components/observability.md) |  |
| `maintenance/obs_probe.py` | [observability](components/observability.md) |  |
| `maintenance/obs_probe_ship.py` | [observability](components/observability.md) |  |
| `maintenance/obs_probe_util.py` | [observability](components/observability.md) |  |
| `maintenance/order_book_lint.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/policy_check.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/prices.json` | [evals-and-report](components/evals-and-report.md) |  |
| `maintenance/pricing.py` | [evals-and-report](components/evals-and-report.md) |  |
| `maintenance/probe_boards.py` | [observability](components/observability.md) |  |
| `maintenance/process_advisor.py` | [role-advisor](components/role-advisor.md) |  |
| `maintenance/product_privacy.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/public_lint.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/quality_nightly.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/release_gate.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/report.sh` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/restore.sh` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/resume_pointers.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/retention.sql` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/retention_transcripts.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/retrieval_eval_job.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/review_rounds.py` | [observability](components/observability.md) |  |
| `maintenance/run.sh` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/sbom.sh` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/scrub.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/secrets_import.py` | [secrets](components/secrets.md) |  |
| `maintenance/spec_lint.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/status.sh` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/templates/README.md` | [maintenance-jobs](components/maintenance-jobs.md) | card and repo templates |
| `maintenance/templates/gitignore-python` | [maintenance-jobs](components/maintenance-jobs.md) | card and repo templates |
| `maintenance/templates/image-rebuild-card.md` | [maintenance-jobs](components/maintenance-jobs.md) | card and repo templates |
| `maintenance/templates/keeper_brief.md` | [lander-and-keeper](components/lander-and-keeper.md) | keeper brief template |
| `maintenance/templates/order-book.md` | [maintenance-jobs](components/maintenance-jobs.md) | card and repo templates |
| `maintenance/templates/pitch.md` | [maintenance-jobs](components/maintenance-jobs.md) | card and repo templates |
| `maintenance/templates/remove-card.md` | [maintenance-jobs](components/maintenance-jobs.md) | card and repo templates |
| `maintenance/templates/renovate-card.md` | [maintenance-jobs](components/maintenance-jobs.md) | card and repo templates |
| `maintenance/templates/repo-conventions.md` | [maintenance-jobs](components/maintenance-jobs.md) | card and repo templates |
| `maintenance/templates/verify.yml` | [maintenance-jobs](components/maintenance-jobs.md) | card and repo templates |
| `maintenance/transcripts.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/usage_guard.py` | [evals-and-report](components/evals-and-report.md) |  |
| `maintenance/vault_git.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover; imported by `capsule/tick.py` and `capsule/brainstorm.py` |
| `maintenance/vault_jobs.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/verdicts.py` | [maintenance-jobs](components/maintenance-jobs.md) | earlier-prototype leftover |
| `maintenance/after_landing.py` | [cos-tools](components/cos-tools.md) |  |
| `maintenance/blind_mutation.py` | [blind-path](components/blind-path.md) | weekly report, install-only job |
| `maintenance/board_query.py` | [cos-tools](components/cos-tools.md) |  |
| `maintenance/branch_sweep.py` | [cos-tools](components/cos-tools.md) |  |
| `maintenance/deps_sync.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `maintenance/disk_relief.py` | [cos-tools](components/cos-tools.md) |  |
| `maintenance/hog_check.py` | [cos-tools](components/cos-tools.md) |  |
| `maintenance/image_gc.py` | [image-record](components/image-record.md) |  |
| `maintenance/image_sweep.py` | [image-record](components/image-record.md) |  |
| `maintenance/landing_prep.py` | [cos-tools](components/cos-tools.md) |  |
| `maintenance/ops_preview.py` | [cos-tools](components/cos-tools.md) |  |
| `maintenance/public_export.py` | [public-edition](components/public-edition.md) |  |
| `maintenance/reviewer_calibration.py` | [evals-and-report](components/evals-and-report.md) | planted-bug calibration of the reviewer |
| `maintenance/storage_gc.py` | [cos-tools](components/cos-tools.md) |  |
| `maintenance/docker_engine.py` | [maintenance-jobs](components/maintenance-jobs.md) | ADDED 2026-11-27: Docker engine table (shell twin lib_docker_engine.sh) |
| `maintenance/lib_docker_engine.sh` | [maintenance-jobs](components/maintenance-jobs.md) | ADDED 2026-11-27 |
| `maintenance/lib_config.sh` | [maintenance-jobs](components/maintenance-jobs.md) | ADDED 2026-11-27: shell spelling of the jobs config dir rule |
| `maintenance/lib_vault.sh` | [maintenance-jobs](components/maintenance-jobs.md) | ADDED 2026-11-27 |
| `maintenance/vault_conf.py` | [maintenance-jobs](components/maintenance-jobs.md) | ADDED 2026-11-27: vault location from capsule.host |
| `maintenance/legacy_gate.py` | [maintenance-jobs](components/maintenance-jobs.md) | ADDED 2026-11-27: LEGACY_PRESENT switch and notice sender |
| `maintenance/public_lint_exempt.txt` | [public-edition](components/public-edition.md) | ADDED 2026-11-27: line exemptions for the public lint |

## bin/ scripts

| Path | Page | Note |
|---|---|---|
| `bin/brainstorm-handoff.py` | [intake](components/intake.md) |  |
| `bin/capsule-compose-recreate` | [compose-live-guard](components/compose-live-guard.md) |  |
| `bin/capsule-keeper-watch.sh` | [lander-and-keeper](components/lander-and-keeper.md) |  |
| `bin/capsule-own` | [lander-and-keeper](components/lander-and-keeper.md) |  |
| `bin/capsule-park.sh` | [deploy-and-drift](components/deploy-and-drift.md) |  |
| `bin/capsule-secret` | [secrets](components/secrets.md) |  |
| `bin/capsule-state-migrate` | [deploy-and-drift](components/deploy-and-drift.md) |  |
| `bin/capsule-suite` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `bin/capsule-tick.sh` | [tick-scheduler](components/tick-scheduler.md) |  |
| `bin/capsule-unblocked-hook.sh` | [dispatch-and-caps](components/dispatch-and-caps.md) |  |
| `bin/hoi-poi.sh` | [deploy-and-drift](components/deploy-and-drift.md) |  |
| `bin/lib_jobs.sh` | [deploy-and-drift](components/deploy-and-drift.md) |  |
| `bin/lib_prefix.sh` | [deploy-and-drift](components/deploy-and-drift.md) |  |
| `bin/new-repo.sh` | [intake](components/intake.md) |  |
| `bin/precheck-gates.py` | [gate-mutation-check](components/gate-mutation-check.md) |  |
| `bin/suite_hook/sitecustomize.py` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `bin/ui-check` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `bin/worktree-only-edits.py` | [gate-protected-paths](components/gate-protected-paths.md) |  |
| `bin/capsule-ui-up` | [deploy-and-drift](components/deploy-and-drift.md) | ADDED 2026-11-27: starts the local UIs |
| `bin/suite_hook/shard.py` | [maintenance-jobs](components/maintenance-jobs.md) | ADDED 2026-11-27: capsule-suite shard worker |
| `bin/suite_hook/sharded.py` | [maintenance-jobs](components/maintenance-jobs.md) | ADDED 2026-11-27: capsule-suite --shards |

## launchd/ jobs

| Path | Page | Note |
|---|---|---|
| launchd/disabled/ (retired author jobs) and the author note-keeping templates | [maintenance-jobs](components/maintenance-jobs.md) | author overlay, not shipped |
| `launchd/templates/__PREFIX__.capsule-corp-backup.plist` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-brainstorm.plist` | [brainstorm-runner](components/brainstorm-runner.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-cleanup.plist` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-deps-sync.plist` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-fork-sync.plist` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-heal.plist` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-keeper-watch.plist` | [lander-and-keeper](components/lander-and-keeper.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-live-snapshot.plist` | [observability](components/observability.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-patrol.plist` | [patrol](components/patrol.md) | every 30 min |
| `launchd/templates/__PREFIX__.capsule-corp-obs-inventory.plist` | [observability](components/observability.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-obs-probe.plist` | [observability](components/observability.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-quality-nightly.plist` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-restore-verify.plist` | [maintenance-jobs](components/maintenance-jobs.md) |  |
| `launchd/templates/__PREFIX__.capsule-tick-capsule-corp.plist` | [tick-scheduler](components/tick-scheduler.md) |  |
| `launchd/templates/__PREFIX__.capsule-corp-blind-mutation.plist` | [blind-path](components/blind-path.md) | install-only |
| `launchd/templates/__PREFIX__.capsule-corp-disk-relief.plist` | [cos-tools](components/cos-tools.md) | every 4 hours |
| `launchd/templates/__PREFIX__.capsule-corp-quality-full.plist` | [maintenance-jobs](components/maintenance-jobs.md) | ADDED 2026-11-27: quality-full job |

## Top-level scripts, docker/, claude/, gate scripts in tests/

| Path | Page | Note |
|---|---|---|
| `docker/build-base.sh` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `docker/IMAGES.lock` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `docker/base/Dockerfile` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `docker/base/worker-entrypoint.sh` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `docker/base/ui-check` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `docker/base/ui_guard.py` | [worker-runner-and-images](components/worker-runner-and-images.md) |  |
| `docker/BUILD_MIN_FREE_BYTES` | [image-record](components/image-record.md) |  |
| `tests/mutation_check.py` | [gate-mutation-check](components/gate-mutation-check.md) |  |
| `tests/mutation_alias.py` | [gate-mutation-check](components/gate-mutation-check.md) |  |
| `tests/mutation_contractdata.py` | [gate-mutation-check](components/gate-mutation-check.md) |  |
| `tests/mutation_info.py` | [gate-mutation-check](components/gate-mutation-check.md) |  |
| `tests/mutation_literals.py` | [gate-mutation-check](components/gate-mutation-check.md) |  |
| `tests/mutation_newsymbol.py` | [gate-mutation-check](components/gate-mutation-check.md) |  |
| `tests/simplicity_check.py` | [gate-simplicity-and-size](components/gate-simplicity-and-size.md) |  |
| `tests/size_check.py` | [gate-simplicity-and-size](components/gate-simplicity-and-size.md) |  |
| `tests/size_baseline.py` | [gate-simplicity-and-size](components/gate-simplicity-and-size.md) |  |
| `tests/size_baseline.json` | [gate-simplicity-and-size](components/gate-simplicity-and-size.md) |  |
| `tests/ruff_gate.py` | [gate-simplicity-and-size](components/gate-simplicity-and-size.md) |  |
| `tests/portability_lint.py` | [gate-simplicity-and-size](components/gate-simplicity-and-size.md) |  |
| `docker/stacks/python/Dockerfile` | [image-record](components/image-record.md) |  |
| `docker/stacks/web/Dockerfile` | [image-record](components/image-record.md) |  |
| `docker/stacks/web/package-lock.json` | [image-record](components/image-record.md) |  |
| `docker/stacks/web/package.json` | [image-record](components/image-record.md) |  |
| `docker/stacks/web/headless-check.js` | [image-record](components/image-record.md) | ADDED 2026-11-27: build-time check |
| `docker/stacks/web/link-browsers.js` | [image-record](components/image-record.md) | ADDED 2026-11-27: build-time helper |
| `claude/capsule-chat/.claude-plugin/plugin.json` | [cos-tools](components/cos-tools.md) | ADDED 2026-11-27: Claude Code chat plugin |
| `claude/capsule-chat/scripts/capsule_chat.py` | [cos-tools](components/cos-tools.md) | ADDED 2026-11-27: status line and read-only commands |
| `claude/capsule-chat/skills/needs-you/SKILL.md` | [cos-tools](components/cos-tools.md) | ADDED 2026-11-27 |
| `claude/capsule-chat/skills/status/SKILL.md` | [cos-tools](components/cos-tools.md) | ADDED 2026-11-27 |

## Contracts, doctrine, templates and compose stack (added with the page set)

| Path | Page | Note |
|---|---|---|
| `contracts/korin.md` | [role-korin](components/role-korin.md) | |
| `contracts/lookout.md` | [role-lookout](components/role-lookout.md) | |
| `contracts/blind-tester.md` | [role-blind-tester](components/role-blind-tester.md) | |
| `contracts/implementer.md` | [role-implementer](components/role-implementer.md) | |
| `contracts/security-engineer.md` | [role-security-engineer](components/role-security-engineer.md) | |
| `contracts/staff-architect.md` | [role-staff-architect](components/role-staff-architect.md) | |
| `capsule/blind/run.py` | [gate-hidden-tests](components/gate-hidden-tests.md) | also `capsule/blind/gate.py`, `capsule/lander_blind.py` |
| `capsule/workspace.py` | [gate-tamper-guard](components/gate-tamper-guard.md) | `PROTECTED` list; also [gate-protected-paths](components/gate-protected-paths.md) |
| `docs/ZENKAI.md` | [zenkai](components/zenkai.md) | protected mirror of the reliability doctrine |
| `launchd/board/__PREFIX__.capsule-tick-__BOARD__.plist` | [tick-board-plist](components/tick-board-plist.md) | per-board tick template |
| `launchd/templates/__PREFIX__.capsule-corp-slack-listen.plist` | [slack-listen-job](components/slack-listen-job.md) | install-only (launchd/unloaded (maintainer only, not in the public edition)) |
| `launchd/templates/__PREFIX__.capsule-corp-docker-idle.plist` | [docker-idle-job](components/docker-idle-job.md) | install-only (launchd/unloaded) |
| `maintenance/docker_idle.py` | [docker-idle-job](components/docker-idle-job.md) | |
| `observability/docker-compose.yml` | [observability-compose-stack](components/observability-compose-stack.md) | eight services |
