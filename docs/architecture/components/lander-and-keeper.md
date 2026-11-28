# Lander and keeper (landing, revert memory)

In short:

- **What it is.** The lander moves a reviewed commit onto `origin/main`.
- **What it does.** It re-checks the exact commit in a throwaway clone and, in `land` mode, pushes exactly that commit. The keeper is the hand-landing loop, with a watchdog.
- **Where the code is.** `capsule/lander.py` (checks), `capsule/landing.py` (push and verify) and `capsule/keeper_watch.py`.

## Purpose
Move a card's reviewed commit onto `origin/main` safely. The lander re-checks the exact commit in a
throwaway clone and, in `land` mode, pushes exactly that commit. It verifies main afterwards and
reverts a push that turns main red. The keeper is the CoS-side human-or-agent loop that lands by
hand when the lander is in `dry-run` or busy; the keeper watchdog pages when it goes silent.

## Where it lives (files)
- `capsule/lander.py`: `run` (the checks), `record` (verdict to event and card state), `guard`, CLI. Never merges or pushes on its own.
- `capsule/landing.py`: the step behind `lander.mode` (`dry-run` default, or `land`): candidates, seat and lock, push, deploy check, verify, revert, revert memory. Called from `Tick._step_pass` (`landing.step`) and as a detached worker (`_spawn`).
- `capsule/landing_rows.py` (candidate rows), `capsule/landing_reverts.py` (crash counting, human-revert detection, wait words, `CRASH_LIMIT`), `capsule/landing_journal.py` (step journal and closing a push left unrecorded), `capsule/diff_ceiling.py` (a card whose real diff is over `diff_lines_park` is parked, not landed), `capsule/lander_blind.py` (the `blind` check in gate mode; see [blind-path](blind-path.md)), `capsule/runners/sandbox.py` (where the candidate's own code runs: the board's worker image, never the host; no docker means a held card; a mac card's checks, and its after-push suites, instead run on the host under the Mac lane profile in a scratch copy, inside the one-job lock `mac-lane.lock`, `runners/mac.py` `session`; so does the `blind` check in gate mode (`lander_blind._on_host`, `mac.run_blind`, the clone read-only, run inside the lane lock the lander's mac session holds, `run.lock`, never taken twice); gate commands written for the worker image get host paths (`mac._host_paths`: `/trusted/tests`, `/workspace`); a lane that is off, down or busy makes that check wait, never run elsewhere. Landing skips a mac card the lane cannot run before it spawns a check (`dispatch.lane_hold`); a check that still waits for the lane writes no `landing_checked`, blocks nothing and counts no failure (`landing._lane_waited`, driven by the structured `mac.LaneWait` in the result's `lane`, not by output text; the card stays `done` and is checked again next pass; Xcode failing mid-landing is the same wait, reason d); a busy lane writes no event, any other reason the one `mac_lane_held`), `capsule/day_branch.py` (see [product-board-landing](product-board-landing.md)).
- `capsule/ci_watch.py`: GitHub CI conclusion for a pushed sha (`gh`, read-only).
- `capsule/keeper_watch.py`, `bin/capsule-keeper-watch.sh`, `launchd/templates/__PREFIX__.capsule-corp-keeper-watch.plist`, bin/keeper-pause (maintainer only, not in the public edition): keeper silence page.
- `bin/capsule-own`: claim or release a branch in the ownership registry (`$CAPSULE_HOME/queue/<repo>/owners.json`).
- `capsule/boundary.py`: marks the landed range and restarts the tick onto it.
- Config: `lander` block (`mode`, `gates`, `needs_docker`, `deploy_check`, `ci_watch`, `ci_wait_s`); example in `capsule/config.capsule-corp.example.json`.

## Inputs and outputs
In: done cards whose latest `completed` event is under 24 hours old, carries a commit and has every review role at pass. Out: `landing_checked` and landing outcome events, a `lander.landed:<card>` guard row, the pushed commit, asks to a human.

## State it reads and writes
`guard_state` keys: `lander.busy` (compare-and-set lock, TTL 3600 s, `seats.LANDER_TTL`), `lander.hold`, `lander.staleask:`, `lander.landed:<card>`, `lander.checking`, `lander.crashed:`, `lander.keeperask:`, `lander.holdask:`, `lander.revertask:`, `lander.pushfail`, `lander.closedfetch:` (fetch back-off for a closed card), `lander.ci` (the CI watch), `lander.last` (see Stall supervisor hooks). Reads the release checkout's `origin`.

## Key rules and invariants
- Check order (`lander.run`): `fetch`, `sync` (commit on the epic branch; a card behind main is replayed by cherry-pick and the new commit is the one landed; a conflict waits), `protected` (PROTECTED paths hold for the maintainer's stamp; a changed file under a `SHA256SUMS` must match its line), `stale_marker`, `gates` (default `mutation_check`, `size_check`, `simplicity_check`; capsule-corp adds `ruff_gate` and `portability_lint` in config), `suites` (the board's `contract_commands`; run in the sandbox; `needs_docker` ones wait while Docker is down; a sandbox with no project image for the repo's declared stacks holds as `needs_docker_tests`, naming the `image build` remedy, and is no docker wait), `blind` (gate mode only: the card's blind tests re-run on the exact commit; a failure or untrusted run holds), `deploy` (`./deploy.sh --check` (maintainer only, not in the public edition), advisory). After a fail or wait the rest are skipped.
- Land path: dry-run writes only the event. In `land` mode: acquire `lander.busy`, check the keeper is not landing (`_keeper_busy`), seats (one lander host-wide, heavy cap), then fetch, push the checked sha to `origin/<lander.branch>` (`main` on the harness board; a product board uses its day branch, see [product-board-landing](product-board-landing.md); never `--force`), run `deploy.sh --check` (maintainer only, not in the public edition), mark the boundary, record `landed`. Deploy drift after a push sets `lander.hold` and asks a human; the hold clears itself when drift is gone.
- After a push, `verify` runs the board suites in a throwaway clone and then GitHub CI (`ci_watch`, when `lander.ci_watch`). Red: open an `escaped_bug` incident once and owe a revert; the revert is made in a throwaway clone of current main with `git revert` of exactly the card's recorded commits and pushed (never forced). A red whose parent was already red is pre-existing: no revert. CI pending past `ci_wait_s` (default 1800 s) asks once and closes the watch.
- Revert memory (`_reverted_on_main`): before landing, the lander walks `origin/main` for a live revert of this completion's change made since the card's latest `completed` event; a revert that was itself reverted is dead. If found, it lands nothing, asks once per sha, and the card stays `done` (outcome `reverted_on_main`). If git cannot answer, it waits (`revert_check_failed`).
- A lander crash is counted; after `CRASH_LIMIT = 3` it stops retrying that card. A detached worker alive over `WORKER_TTL = 3 h` is treated as stuck.
- Keeper watchdog: pages (outbox row) when the keeper log is silent for over `SILENT_AFTER = 3600` s while work waits; bin/keeper-pause declares a deliberate pause (capped at 12 h). It only pages.

- Public clones: the lander's deploy check runs `./deploy.sh --check` by default (`DEPLOY` in `capsule/lander.py`; the board config key `lander.deploy_check` replaces it). deploy.sh is maintainer only, so a public clone does not have it. A non-zero exit counts as drift (`capsule/landing.py`, `_deploy`), so a public board should set `lander.deploy_check` to its own check.

## Failure modes and what happens
Fetch failure: waits. Push rejected as non-fast-forward: outcome `push_rejected`, retried. Hook or branch-protection rejection: `push_failed`. Suites red before push: card goes back through the failure path (`lander.record`). Docker down: held `lander_wait_docker`.

## Tests that pin it
`capsule/tests/test_lander.py`, `test_lander_env.py`, `test_lander_guard.py`, `test_lander_hold_alarm.py`, `test_landing.py`, `test_landing_step.py`, `test_landing_red.py`, `test_ci_watch.py`, `test_keeper_watch.py`, `test_boundary.py`, `test_ruff_gate.py`, `test_shell_portability.py`; `tests/test_lander.sh`, `tests/test_keeper_watch.sh`; `contracts/intent/lander.jsonl`, `lander_revert.jsonl`, `land_reliability.jsonl`, `keeper_watch.jsonl`.

## Stall supervisor hooks
`landing.step` writes guard `lander.last` `{word, since, at}`: the pass's word and how long it stayed the
same (`stall_remedies.lander_stuck` reads it). A `lander.busy` claim held by a card with 3 waits
(`landing_checked` verdict `wait`, or recheck `stall_action` events) is released, so the next approved card
can land. The stuck card is rechecked each 6 h by the supervisor and is never forced through.

## Related decisions and specs
Row 600 (lander mode); plan C07a to C07c; LAND-07 (row 171); revert memory (decided 2026-10-28); docs/superpowers/specs/2026-09-06-merge-gate-design.md (maintainer only, not in the public edition) (the earlier merge gate this replaced); this page.

## Status
current. `lander.mode` defaults to `dry-run`; the keeper-watch job (and the blind-mutation job) is install-only: listed in launchd/unloaded (maintainer only, not in the public edition) until the maintainer removes the line and runs ./deploy.sh. Approval of an epic is the owner's GitHub merge of its PR; the old terminal approval and the `merge_gate.py` per-card pipeline were retired 2026-11-12 (D238, this page). After a keeper landing the CoS runs `maintenance/after_landing.py` (and `landing_prep.py` packs reviewed branches into a keeper batch); see [cos-tools](cos-tools.md).

After-landing and deploy (D99): the tick restart decision uses the code each running tick loaded, not the release clone checkout. `capsule tick` records `<pid> <sha>` in `$CAPSULE_HOME/tick-<board>.sha` (`_cmd_tick`); `capsule-tick.sh status` reports it as `loaded_sha`; `maintenance/after_landing.py` restarts a board when tick-code paths differ between that sha and origin/main, or when no sha is recorded (old process).
