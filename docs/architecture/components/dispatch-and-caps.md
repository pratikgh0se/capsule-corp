# Dispatch, seats and caps

In short:

- **What it is.** Dispatch picks which ready card may start next.
- **What it does.** It enforces seats (how many heavy jobs run at once on the machine) and caps (running, runtime, failures).
- **Where the code is.** `capsule/dispatch.py` and `capsule/seats.py`.

## Purpose
Decide which `ready` cards may be claimed this tick, how many heavy seats (implementers, live
reviews, the lander) are in use across all boards, and how to keep epic clones and the
unblocked-work signal honest.

## Where it lives (files)
- `capsule/dispatch.py`: `ready`, `_brief_held`, `admit_reviews`, `hold`, `clone_dirty`, `screen_held`, `board_paused`, `any_pause`.
- `capsule/seats.py`: host-wide seat snapshot across `$CAPSULE_HOME/boards` (`REVIEW_PROFILES`:
  reviewer, validator, guide, korin, blind-tester, security; there is no `fable` profile, the Fable
  seats live in the maintenance epic lane and agent-review only); `limits`, `heavy_cap`,
  `implementer_count`.
- `capsule/dispatch_gate.py`: per-board dispatcher config and liveness.
- `capsule/config.py`: `caps` defaults (`CAP_DEFAULTS`) and keys.
- `capsule/epic_clean.py`, `capsule/epic_sync.py`, `capsule/epic_graph.py`: epic clone hygiene (the lane-hold alert moved to [stall-supervisor](stall-supervisor.md); `lane_holds` stays), sync
  with `origin/main`, dependency graph page. `tick_runs._guard_launch` (using `epic_clean.set_aside`) runs before a card launches
  on a reused clone: dirt that is not that card's own (owner = the card with the newest unsettled
  run on the branch) is backed up to `epic-backups` and discarded, with a `lane_leftover_set_aside`
  event and a `running` notice naming owner, backup and paths. If the backup fails nothing is
  discarded, the launching card goes `needs_input` (`worktree_dirty`), and `dispatch` holds the
  lane's other cards while the `epic clean backup failed: <branch>` incident is open; one incident
  and one notice (naming up to 10 stray paths) per lane while it stays open.
- `capsule/lane_behind.py`: Z5 lane-behind detector (L155). `epic_sync.sync_idle` measures each held epic clone against main (`_note_held`, private ref `LANE_MAIN`, at most every `CHECK_S` = 10 min) and `note`s it; `statusview._lane_behind_problems` shows it. Detection only; see this page. The implementer brief's `## Lane behind main` section is separate (`worker._behind_main`, `bundles._lane_behind_section`).
- `capsule/unblocked.py`, `capsule/unblocked_stop.py`, `capsule/idle_lane.py`, `capsule/metrics.py`, `capsule/cardmeta.py`, `bin/capsule-own`, `bin/capsule-unblocked-hook.sh`: the unblocked/unowned-work detector and its dead-man alert.
- `capsule/assign.py`: `capsule human ... assign` so a card has a routable assignee.

## Inputs and outputs
In: board DB, config, other boards' DBs (read-only). Out: the list of ready rows (`dispatch.ready`), review admissions, `review_deferred` events.

## State it reads and writes
Reads `tasks`, `task_runs`, `guard_state` (`pause:board`, `pause:account:<a>`, `lander.busy`, `tick_restart_pending`). Writes only events (`review_deferred`, `design_brief_missing`, `needs_missing`, `mac_lane_held`).

## Key rules and invariants
`ready` returns nothing when any of these holds: running implementers at `caps.max_running`;
unreadable board or heavy seats at `max_heavy - external_heavy`; board paused or held at the weekly usage line (`weekly_pause`); tick restart in flight (`_board_stopped`); no epics configured. The result then passes through `host_guard.gate`, which drops strong-tier cards while the disk or failure-storm guard holds.
Per card it skips: another card already running or in review on the same branch (serial within a branch, row 400); a card with an unlanded parent when `redesign.deps_ready` is on; a risky card whose design brief is missing (`capsule/design_briefs.py`; one `design_brief_missing` event per card, freed when the file appears); a card whose body `needs:` line names a path absent in the epic clone (`dispatch.needs_missing`; one `needs_missing {path}` event per card per path; stays `ready`, no run, no incident, freed when the file appears; a plan card gets the line from its `Needs:` field); a dirty shared epic clone held by another card; the epic's first screen held; an assignee with no route; a tier the host cannot run (a mac card whose Mac lane is down also gets one `mac_lane_held {reason}` event per reason, and stays `ready`; reasons: switch off, not macOS, no sandbox-exec, no Xcode Swift toolchain); a paused account (the restart route's own account when the ladder applies).
Order: `priority DESC, created_at ASC`, except that the fix card of an open incident goes first (`_fix_first`, so a red base clears first); a card with a pending `stop` is skipped (`stop_queue.PENDING_SQL`). Defaults in `CAP_DEFAULTS`: `max_review` 2, `max_heavy` 3, `max_runtime_seconds` 3600, `review_seconds` 1800, `external_heavy` 0 (the example config sets `max_review` 2, `max_heavy` 3 and `external_heavy` 1). `max_running` has no default: the board config must set it.
`capsule unblocked` reuses dispatch's own predicates so its blockers cannot drift from `ready`. `idle_lane` flags a board with no live seat and a ready card the dispatcher cannot take for 15 minutes (`IDLE_S`). `stall.find` reads it as class `lane_idle`. Its result carries `hold` (`branch-busy` or `clone-dirty`, the stable key) and `label`, which names a blocked or triage holder honestly: `branch held by <task id> (blocked: <kind>)`. The alert for that case is the stall supervisor's `lane_idle` item ([stall-supervisor](stall-supervisor.md)). A review holder with a live contract job is not a hold: `idle_lane.contract_live` reads the newest `contract_started` with no later `contract_timeout` or `contract_failed` and no run row of its `run_id`, under 2 hours; `no_progress` and the unexplained-ready check count it as an open run.

## Failure modes and what happens
Reviews that cannot get a seat wait with one `review_deferred` event per standing deferral. An unreadable board anywhere holds all heavy seats and raises an incident after an hour (`seats.HOLD_INCIDENT_AFTER`).

## Tests that pin it
`capsule/tests/test_dispatch.py`, `test_main_dispatch.py`, `test_seats.py`, `test_external_heavy.py`,
`test_unblocked.py`, `test_unblocked_assignees.py`, `test_unblocked_dependency.py`, `test_unblocked_dispatcher.py`, `test_unblocked_done.py`, `test_unblocked_item_schema.py`, `test_unblocked_locked_board.py`, `test_unblocked_one_store.py`, `test_unblocked_ownership.py`, `test_unblocked_park.py`, `test_unblocked_stop.py`, `test_unblocked_vault_eval.py`, `test_idle_lane.py`, `test_unowned_alert_rule.py`, `test_epic_clean.py`,
`test_epic_sync.py`, `test_epic_sync_idle.py`, `test_epic_graph.py`, `test_assign.py`, `test_metrics.py`; `contracts/intent/dispatch_caps.jsonl`, `unblocked*.jsonl`.

## Related decisions and specs
`2026-09-24-capsule-corp-2-design.md` section 6.7; rows 369, 400, 470, 483, 624; this page.

- Weekly pause (D234): `capsule/usage_meter.py` estimates the share of the weekly limit spent from the Claude Code transcripts; `capsule/weekly_pause.py` makes the tick start no new card runs at or past `usage_pause_at` (default 0.90). Runs in flight, reviews, rulings and landings go on.

## Stall supervisor remedies (unit 3)
`idle_lane.find(..., idle_s=)` takes a 10 min limit when no run is open (`lane_idle`). A silent worker
(no spool or workspace file change for `stall.silent_s`, no CPU growth) is killed by the supervisor when
`stall.silent_kill` is true (default): the run ends `timed_out`, at most 2 kills per card in 24 h. A card
with no branch and one declared epic gets the default branch and assignee. See
[stall-supervisor](stall-supervisor.md).

## Set-aside cards (stall supervisor, unit 6)
`epic_clean.holders` (and so `lane_holds`, `held` and `dispatch.clone_dirty`) skips a card with an
`aside:<card>` row. The lane runs its other ready cards. A lane with no ready or live card for 30 min
releases the oldest held Korin fix card, `unattended.fix_release_per_day` a day (unset: 0). See
[stall-supervisor](stall-supervisor.md).

## Status
current.
