# Tick scheduler

In short:

- **What it is.** The tick is the loop that runs the factory for one board.
- **What it does.** Each pass it reads the board, starts and closes runs, runs reviews and lands finished cards. It never calls a model.
- **Where the code is.** `capsule/tick.py` (with `tick_runs.py`, `tick_review.py`, `tick_rulings.py`).

## Purpose
The tick is the factory's scheduler and the only writer of a board's SQLite file. Each pass it
reads config and human files, repairs stale runs, applies finished runs' documents, runs the review
stage, rulings and incidents, dispatches ready cards, lands completed cards and flushes
notifications. It never calls a model (`capsule/tick.py` module docstring).

## Where it lives (files)
- `capsule/tick.py`: `class Tick(TickRuns, TickReview, TickRulings)`; `Tick.once` and
  `Tick._step_pass` carry the exact step order.
- `capsule/tick_rows.py`: free functions that read decision rows and spec sections out of note text
  for the guide. `capsule/tick_runs.py` (`TickRuns`): leases, process liveness and kill, clones,
  claim-and-launch, worker spawn. `capsule/tick_review.py` (`TickReview`): review dispatch, first
  screen, open findings, deferred contradictions (`REVIEW_ROLES`, `REVIEW_TRIES`).
  `capsule/tick_rulings.py` (`TickRulings`): implementer-report router, git-lock notes, intent
  contradictions, the guide's ruling packet and history. The new modules never import
  `capsule.tick`; `capsule.tick` re-exports every `tick_rows` name.
- `capsule/tick_guard.py`: per-board exclusive file lock `tick-<board>.lock` under `$CAPSULE_HOME`; `held` is a read-only probe.
- `capsule/config.py`: board config load, validation and defaults; `Loader` re-reads per pass.
- `capsule/paths.py`: `CAPSULE_HOME` resolution and per-board paths.
- `capsule/boundary.py`: restarts the tick onto landed code (`bin/capsule-tick.sh restart`).
- `capsule/preflight.py`: `python3 -m capsule check`, read-only preconditions for a live run.
- `capsule/invariants.py`: detectors for `main_checkout_write`, `lost_card_state` (off unless `invariants.enabled`). `capsule/docker_down.py` and `capsule/exec_outage.py`: Z5 detectors for Docker down with cards waiting and a lost exec daemon.
- `capsule/cli_ops.py`: `capsule pause` / `unpark`; `capsule/__main__.py`: CLI entry (verbs in the `DISPATCH` table: tick, run-one, report, check, unblocked, stalls, human, human-apply, incidents, image, vault-context, lookout, status, board, intake, plan, prior, outbox-send, card, pause, unpark, names, cos, golden, tokens, tick-watch, veto, patrol, and more; `capsule --help` lists them).
- `bin/capsule-tick.sh`: launchd start/stop/restart/status per board; `launchd/templates/__PREFIX__.capsule-tick-*.plist`.

## Inputs and outputs
- In: `boards/<board>/config.json`, the board DB, `human/` and `intake/` files, spool dirs of finished runs.
- Out: board DB changes (only through `capsule.db.transition`/`event`), spawned `run-one` processes, outbox rows, Lookout spool lines, heartbeat.

## State it reads and writes
Reads/writes `boards/<board>/kanban.db` and `guard_state` rows (pauses, `tick_restart_pending`,
`lander.*`). Writes `spool/<run_id>/job.json`. Reads other boards read-only for seats.

## Key rules and invariants
- `once()` runs first: migrate the board once per process, then the Docker engine wake check (a probe error counts as not up), before `_step_pass`.
- Pass order (`_step_pass`): Slack flag; digest; config-error notice;
  intake and human interventions (`intake.step`); reap orphan containers; timeouts; leases and
  heartbeats; close runs from spool; stall continues (`stall_continue.step`); queued `stop` verbs (`stop_queue.sweep`); late blind-test results (`blind_dispatch.late`); review;
  guide rulings; ruling sweep; incidents (Korin); epic clean then sync of idle clones; dispatch;
  blind-tester dispatch (`blind_dispatch.step`); metrics export; stall supervisor (`stall.step`: stalls, the unowned-idle
  check, the docker-down check); missing-image requests (`image_requests.step`); exec-outage release (`exec_outage.release`); landing; boundary.
  Afterwards, in `finally` blocks: outbox flush, heartbeat, Lookout.
- A config that fails validation keeps the last good config and raises an incident notice; a config
  naming a different board than the pinned one gives no pass (`config_error`).
- `tick_guard` refuses to take the lock for an unconfigured board.
- Counts per pass (`COUNTS`) are reported by the heartbeat.
- Code freshness (D99, so the tick never runs old code after a landing): before each pass `capsule tick` checks whether the tick-code paths differ between the sha it loaded and the clone HEAD. Stale and quiet (no open run, child worker, lander worker or validator job; a board with no `kanban.db` yet counts as quiet and the file is not created): log `tick_stale_restart`, exit 75. Stale and busy: the `tick_health` payload has `code: "stale (code <loaded> vs <head>), restart waits: busy"` (12-char shas). Fresh: `code: null`.
- Redesign switches in `config.redesign` default off (`config.REDESIGN_DEFAULTS`). Boolean switches with a consumer: `deps_ready` (`dispatch.py`), `check_gate` (`check_gate.py`), `escalation` (`restart.py`), `flake_reruns` (`flake.py`), `interrupts` (`outbox.py`), `light_runner` (`runners/host.py`), `mac_runner` (`runners/mac.py`), `security_review` (`security_review.py`, needs `routing.security`). Accepted by validation with no consumer in `capsule/` (the status views show lanes whatever it says): `lanes`, `night_runner`, `memory_rules`, `advisor_cards_only`. Three-mode switches: `allocator` (`off`/`report`/`live`, no consumer), `vault_context` (`off`/`report`/`live`) and `blind_tests` (`off`/`report`/`gate`). `image_loop` is not a switch: its only value is `record` (see worker-runner-and-images).

## Failure modes and what happens
- Exception in work on one card: `_guarded` records a `tick_error` event and one incident-class outbox line (once per card and message); the incident step turns the event into a `crash` incident; the pass continues.
- Docker down: strong tier unavailable, so strong cards are not dispatched (`host_tiers`).
- Two ticks for one board: the lock refuses the second; the lock is the only guard. `two_dispatchers` is an allowed incident kind, but `invariants.sweep` does not probe for it.
- After a landing, `boundary.step` waits for a gap and restarts the tick; dispatch claims nothing while a restart is in flight.

## Tests that pin it
`capsule/tests/test_tick.py` (frozen length), `test_tick_step_order.py`, `test_tick_runs_split.py`,
`test_tick_review_split.py`, `test_tick_rulings_split.py`, `test_tick_guard.py`,
`test_board_tick.py`, `test_boundary.py`, `test_config_autonomy.py`, `test_redesign_switches.py`,
`test_preflight.py`, `test_invariants.py`, `tests/test_capsule_tick_sh.sh`.

## Related decisions and specs
docs/superpowers/specs/2026-09-24-capsule-corp-2-design.md (maintainer only, not in the public edition) (section 2, 6.4, 6.5),
`2026-09-27-factory-autonomy-design.md` (two ticks, spec 4.3), rows 376, 377, 517; the operating topics (tick era, two ticks under launchd, boundary restart, invariant incidents) are covered in this page, [lander-and-keeper](lander-and-keeper.md) and [incidents-korin-flow](incidents-korin-flow.md).

## Status
current.
