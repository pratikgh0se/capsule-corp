# Z5 detectors (docker down, exec outage, invariants)

## Purpose
Zenkai Z5 says a stopped factory with no fix in hand must tell the owner instead of failing
quietly. These small tick steps detect a failure the loop itself cannot see and turn it into an
incident and one alert.

## Where it lives (files)
- `capsule/docker_down.py`: Docker unavailable while cards wait on a container, for `DOWN_S` (15 min, or `docker_down_s` in the board config) or longer, opens one incident and sends one alert. Called by `stall.step` once per pass with the strong runner's own availability answer (no second probe).
- `capsule/exec_outage.py`: a docker daemon lost while a strong-tier worker ran. The exec tool (`runners/exec_mcp.py`) writes `<spool>/exec_outage.json`; `hold` blocks the held report as `transient` with cause `exec_outage` (nobody has anything to answer); `release` lifts it on the first pass with docker up, keeping `block_recurrences` so a second outage on one card goes to triage.
- `capsule/invariants.py`: `sweep` checks `main_checkout_write` and `lost_card_state` and runs only when `invariants.enabled` is set (it needs `invariants.dev_checkout`). Only the tick running with its working directory in the dev checkout pauses the board (`pause:board`, until the maintainer clears it). A dirty dev checkout and `lost_card_state` raise an incident and a `needs_input` notice and do not pause. `two_dispatchers` is an allowed incident kind (`invariants.KINDS`) but `sweep` has no probe for it.
- `capsule/seats.py` (`HOLD_INCIDENT_AFTER`), `capsule/idle_lane.py`, `capsule/unblocked_stop.py`: unreadable-board hold, idle lane and unblocked-work detectors ([dispatch-and-caps](dispatch-and-caps.md)).
- `capsule/stall.py`: the stall supervisor. `stall.step` runs once per pass (it calls `docker_down.step` and the dead-man check `unowned_idle.check`) and finds held cards (triage and blocked), idle and unroutable lanes and a board with no progress. One record (`guard_state` `stall:<class>:<subject>`) and one incident per stall; the owner gets a Mac notification each 30 min and a Slack page at the start and each 2 h. See [stall-supervisor](stall-supervisor.md). The lane-hold alert moved there (the `lane_idle` class): `epic_clean.lane_holds` stays as read-only evidence.
- `capsule/keeper_watch.py`: keeper silence ([lander-and-keeper](lander-and-keeper.md)).

## Inputs and outputs
In: the pass's docker availability, spool markers, board state. Out: an incident, an outbox alert, a block or release.

## State it reads and writes
Guard `docker_down` (`{since, waiting, fired_at}`); `block_kind` of the held card.

## Key rules and invariants
`docker_down` clears only after docker has been back for `UP_PASSES` (2) passes in a row (a one-pass flap keeps the clock). `exec_outage` never blames the card: it is an infrastructure hold, not a failure round.

## Failure modes and what happens
Each detector raises its own incident ([incidents-korin-flow](incidents-korin-flow.md)).

- `capsule/tick_watch.py`: the watchdog of the tick, run by `heal` (and by hand: `capsule tick-watch [--apply] [--json]`). `decide(beat_age_s, loaded, dispatcher, state, now)` gives ok, wedged (beat older than 10 min), dead (`tick_guard.dispatcher` says `none`), stopped (plist not loaded: start it, unless the stop marker `<board dir>/tick-stop.json` is valid; no marker and no `stop_marker` given: page) or crashloop (a third restart inside 1 h: page, then one restart per 6 h and one `rollback` of the release checkout to `last_good_sha`; a rollback needs an ancestor, the same schema, a clean tree and no live lander, and opens an incident). It reads `<board dir>/tick.beat`; with no beat file `wedged` is never given (only dead and stopped act), and for 10 min after a restart a board is not judged wedged. `--apply` restarts the tick once through `bin/capsule-tick.sh restart <board> --no-update` (the release checkout does not move) and pages from the Mac (`osascript`; under `AGENT_OPS_NO_SEND=1` one line in `local-no-send.jsonl`) plus one `outbox.log` line, repeated each 30 min. State: `<board dir>/tick-watch.json`.
## Tests that pin it
`capsule/tests/test_docker_down.py`, `test_exec_outage.py`, `test_invariants.py`, `test_seats.py`, `test_idle_lane.py`, `test_lane_hold_alert.py`.

## Related decisions and specs
D159 (docker down), inc_0221ae04 (exec outage), plan C15b; `docs/ZENKAI.md` Z5.

## Status
current.
