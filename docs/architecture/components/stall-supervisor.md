# Stall supervisor (held cards, lanes, the page that repeats)

## Purpose
A stall is work that can move and did not move for longer than its threshold. The supervisor finds
each stall, keeps one record and one incident for it, and tells the owner until it clears. The full
design, with the classes that later units add, is the internal stall-supervisor design note (not shipped).

## Where it lives (files)
- `capsule/stall.py`: `find`, `step`, the detectors, the records and the L2 page.
- `capsule/stall_owner.py`: set aside, decide alone, the never list, `veto` (unit 6).
- `capsule/stall_cli.py`: `python3 -m capsule stalls [--board B] [--json] [--capsule-home H]`.
- `capsule/unowned_idle.py`: the `unowned-work-idle` dead-man check, moved out of `Tick` (`check`).
- `capsule/outbox.py`: channel `local` and `send_local` (see [outbox-notifications](outbox-notifications.md)).
- `capsule/host_guard.py`: unit 4b host guards: disk and the failure storm (`failure_storm`, G9). `capsule/log_caps.py`: the log-cap check.
- `capsule/config.py`: the `stall` block (`config.stall`). `capsule/event_kinds.py`: `stall_opened`, `stall_cleared`, `stall_action`.

## Inputs and outputs
In: the board file, the config and `now`. `find(conn, cfg, now, probes=None)` is read-only and returns a list
of dicts: `class`, `subject`, `since`, `age_s`, `level`, `evidence`. `probes` may carry `work_root`.
Only stalls past their grace time are returned. `level` is `L2` once the stall may page, else `watch`.
`step(tick, conn, cfg, strong_up, now)` is the tick's one call. It runs `docker_down.step`, the
dead-man check and the stalls. It never raises.
Out: `guard_state` records, `stall_opened` and `stall_cleared` events, incidents and outbox rows.
`capsule stalls` prints the same items as `find`. Exit 0 with none at L2, 1 with one or more at L2, 2 with no board file.

## State it reads and writes
Record: `guard_state` key `stall:<class>:<subject>` with `since`, `incident`, `paged_at`, `pages`,
`slack_at`, `level` and `evidence`. The subject is a card id, or the board name for `no_progress`.
The record is deleted when the stall clears. Events about the board itself use the task id `stall`.

## Key rules and invariants
- Classes in unit 1: `held_triage` (5 min), `held_blocked` (5 min), `held_parked` (15 min when the card
  holds a lane with ready cards behind it, else 7 days, never L2), `owner_wait` (0 min; L2 after 20 min
  only if a lane waits or the board has no open run), `lane_idle` (`idle_lane.find`, 15 min),
  `unroutable` (15 min, detect only), `no_progress` (only while a card on an epic branch is ready, running, claimed or in review; 45 min, L2 at once, an open run counts as progress
  until its 65 min cap; while a contract check is live and no open run is older than 65 min, the limit is 2 h from the newest board activity).
- The clock of a card hold is its newest hold event (`blocks.HOLD_EVENTS`). `waiting: N` in the evidence is
  the count of ready cards on the same branch.
- A holder card gives one item. `lane_idle` never repeats a holder that has its own stall.
- One incident per stall: the card's open cause incident gets a `stall:` evidence line; if there is none,
  one `hang` incident opens (key `stall: <class>: <letters>`, `stall: unknown: <board>` for `no_progress`).
  `owner_wait` opens none (the owner is the fix).
- L2: when the age reaches `l2_deadline_s` (default 20 min), `outbox.notify(page=True)` writes the `file`,
  `slack` (if Slack is on) and `local` rows. A `local` row repeats each `repage_s` (30 min, any hour). A
  Slack row repeats each `slack_repage_s` (2 h). The clock is `now - since` and never restarts.
- Scope: the card classes consider only cards whose `branch_name` is in `cfg["epics"]` (`_active`); other
  cards get no record, event or page. `lane_idle` and `no_progress` are unchanged.
- Batching: `_page` queues each due page and `_flush` sends them once per pass. One page is sent as above.
  Two or more: one `file` row each, one `local` row and (if any is due for Slack) one Slack row, text
  `<board>: N stalls. <class "card title" clock>; ... ; +K more` (first 3 named, titles cut to 40).
- Quiet under any pause (`dispatch.any_pause`): `find` returns nothing, `step` keeps the records and pages nothing.
- A detector that raises does not stop the others. `step` opens one `crash` incident per 24 hours (key
  `stall supervisor: <ExceptionType>`) and clears no record in that pass.

## Unit 2: the L1 stall ruling
File: `capsule/stall_ruling.py`. One guide ruling per stall of class `held_triage` or `held_blocked`, on the
existing ruling path (`Tick._ruling_one`, schema `decision.v1`). No new role and no new route.
- `step` (called by `stall._run` after the L0 remedies) writes one `stall_ruling` event for each eligible
  held card. `wanted` and `pending` let the tick find it. A `review_infra` block waits for its one L0 re-run.
- `eligible(conn, cfg, task, now) -> (bool, reason)`. Never for a park, `spec_stale`, a `decision` card,
  `owner_rounds`, a safety finding, a card the guide already sent to the owner, or a pending guide pass.
  Caps: 1 release per card in 24 h, 2 since the last human verb, `stall.rulings_per_day` (6) per board in 24 h.
- `packet`: the stall class and age, the hold, the finding lines with the validator exit codes,
  `suspect_infra` (the only failures are validator rows with exit 0), the cost, the earlier rulings and the
  question "Whose fault is this hold: the card, or Capsule?" Options: `bundles_guide.STALL_OPTIONS`.
- `release`: `blocks.reset_failures(by='capsule')` on a triage card, `blocks.unblock(by='capsule')` on a
  blocked card. One `guide_ruling` event `{role: 'stall', choice, fault: 'card', applied}`. The note reaches
  the implementer through `Tick._guide_rulings`. `by='capsule'` never refills the runaway budget: the card
  gets exactly one more run, then the owner.
- `release-capsule-fault`: the same release with `fault: 'capsule'`. The defect text joins the stall's
  incident and one `file` line is written. No page.
- `ask-owner`: nothing is released. The document must carry `provisional` (`release`,
  `release-capsule-fault` or null) and `reversible` (bool). `apply` stores `ruling`, `provisional` and
  `reversible` in the record `stall:owner_wait:<card>` (keys unit 6 reads) and the guide's question is the
  page text. A spent ladder (a stall ruling in the last 24 h, or a failed one) pages at once
  (`stall._spent`), not at the 20 min deadline.
- Two invalid ruling documents: one `ruling_failed` event, no release, L2 at once, one `silent_failure`
  incident with key `stall ladder failed: <class>` (the second one in 30 days after a close is a `repeat`).
- A supervisor action writes no `intervention` event and opens no `human_intervention` incident.
- An `amendment` in the document is not applied by this unit.

## Unit 3: runs, review and landing (L0 remedies)
Files: `capsule/stall_remedies.py` (detectors, remedies, `step`), `capsule/stall_probes.py` (the injectable
process and file facts). `stall.find(conn, cfg, now, probes)` and `stall.step(..., probes=)` take
`alive(pid)`, `quiet_s(run_id)`, `cpu_s(pid)`, `kill(pid)`, `docker_up` (bool or callable), `sleep`,
`spool_root`. A tester injects them and needs no real process. `disk_free` belongs to unit 4b (G6).
- `run_silent` (C1): an open run with a live pid, no spool file or workspace file newer than `silent_s`
  (review roles: half of it) and under 1 s of CPU growth (`CPU_GROWTH_S`) over a window as long as that quiet limit, read from stored CPU samples taken at least 300 s apart (`SAMPLE_GAP_S`; `capsule/stall_silence.py`). CPU
  unreadable is observe-only: the item shows, nothing is killed. A container run (runner `strong`: its
  commands run in the container, so their CPU is not in the host tree) is never killed and never
  sampled: it shows at watch and pages at L2 only. Killable: host runs (reviewer, validator, guide,
  korin, light and mac tiers) on this host. L0 (`stall.silent_kill`,
  default true): kill the worker, end the run (status `timed_out`, outcome `run_silent`, error text has `silent`, the `timed_out` event
  has `kind: run_silent`), no failure counted (an implementer card goes `running` to `ready`, claim released), one `stall_action` `kill`. At
  most 2 kills per card in 24 h; a third silent run goes to L2. With the switch off the item pages at L2.
- `run_unspawned` (C3): a review-role run row with no pid for 5 min. L0 ends it `spawn_failed`; the next
  review pass starts a new run.
- `state_orphan` (C4): `running` or `claimed` card, no open run, claim ended 5 min ago. L2 at once, no change.
- `review_deferred` (D1): newest events are `review_deferred` for 30 min (stall events do not break the
  run). Evidence lists the seat holders. L0 clears a `lander.busy` flag past its TTL.
- `held_blocked` with `block_kind='review_infra'` (D2): L0 `blocks.unblock(by='capsule')` once: the card
  goes back to `review` on its last implementer run (`request_review`) so only the review roles re-run. A
  second `review_infra` block gets no second re-run.
- `landing_stuck` (E1): a `pushed` landing held 30 min, or a lander wait unsettled after 30 min or 3
  rechecks (L2). `lander_stuck`: `lander.last` has the same word, not idle, running or off, for 30 min.
  L0 recheck (any pass, every card): a `lander_wait_docker` card goes back to `done` when Docker is up, a
  fetch or env wait after 30 min; 3 times (10 min apart), then once per 6 h. Never a forced landing.
- B3: `route_defaults` runs before detection: one declared epic, a ready or todo card with no branch
  or assignee gets the default (`epic_assigned`).
- `no_progress` also fires at 10 min when a ready card, no open run, no pause and no `lane_idle` reason
  exist; `lane_idle` uses 10 min when no run is open. Items may carry `reason`.
- L0: 2 tries per stall, 10 min apart, one `stall_action` event each; the record keeps `l0`, `l0_at`.
  The card classes of this unit follow the same `epics[]` scope as unit 1.

## Quiet stops and continue
Design: the unattended-mode design (section 9.10; that note is not in this repository). Code: `capsule/stall_continue.py`; `stall.step` calls
`stall_continue.step` before the stall passes. When an agent stops with nothing decided, Capsule continues the
card by itself. It does not wait for a person.

`on_close` (called by `Tick._close_one` for each closed implementer run) writes one `run_closed` event: the
`before` and `after` progress marks (`head`, `block_kind`, `findings`, `verdict`, `check`), the run outcome,
the `session_id` and the `kind`. It also stores the model session id in `task_runs.session_id`. The worker
writes `last_text` (the last 600 characters of the model result) in `run.json`.

`kind(run, before, after, handoff, last_text)` returns one of: `run_harness` (the handoff or the block names a
step the harness does itself, now `untrack`), `run_asked` (a `question` in the handoff, a `needs_input` block
that ended the run, or a last text that asks while the head did not move or the check fails), `run_early` (a
clean end under 10 % of the time cap with a failing check or a `left_undone` list), `run_empty` (the head did
not move and the handoff has no verdict, block or answer), `run_same_state`, or `None`. A run with an
`error_class` or a non-zero `exit` is never quiet: the failure paths own it. `run_silent` is not set by
`kind`: the silent-run detection ends the run with `outcome='run_silent'` and `step` handles it as a quiet stop.

`step(tick, conn, cfg, now)` handles the newest quiet close of each card once. Ladder: (1) resume, a new run in
the same model session (`claude -p --resume <id>`, provider `claude`, a session id, the branch head unchanged);
(2) re-dispatch, a fresh run with the missing text as a `guide_ruling` note (`choice: continue`, read by
`Tick._guide_rulings`); (3) one run on the stronger route (`implementer_restart`, only when configured);
(4) set aside: `guard_state` `aside:<card>` and one incident with "quiet stop: continue failed". Caps: 2
resume or re-dispatch continues per card and 30 per board (`unattended.continues_per_day`) in 24 h. A run that
makes progress (its work mark moves) resets the card for the ladder and for the per-card 24 h cap. The work mark
is a hash of the clone diff against `start_sha` (tracked and untracked files, the diff the review sees), never
the git HEAD: the harness commits only after the review gate. A card in `review` takes no continue once a
review-role run or a contract job started, and a `set aside` card takes none until a human `unblock` or
`reset`. A run a continue moved out of review opens no review round (`stall_action.from_run`). If a resume
fails, the worker runs the prompt once as a fresh run (the runner names no missing-session error). `step`
does nothing while `pause:board`, an account pause or `weekly_pause` is held. `step` only prepares the card (back to `ready`, a plan in
`guard_state` `continue_plan:<card>`); the normal dispatcher starts the run, so every pause and lane rule
still holds. `_launch` puts the session id in `limits.resume` and the prompt in `inputs.continue_prompt`, and
the worker sends that prompt; then it clears the plan. No continue starts for a card in `triage`, a
`parked`, `spec_stale`, `runaway` or `claims` hold, with a decision request, a safety finding, an open run, or
when a later run exists. A continue writes no `failed` event, so `runaway` counts nothing.
`missing(conn, task, run)` builds the text from facts: the unchecked `- [ ]` lines of the card, `left_undone`,
the question plus "Nobody is here to answer" for `run_asked`, the harness sentence for `run_harness`.

## After L2 (unit 6: set aside, decide alone)
The owner is never on the path that keeps the factory moving. `capsule/stall_owner.py` runs in the
same tick pass, after the page. Each step is a script. No model runs.
- Set aside. A card stall that reaches L2 gets one `aside:<card>` row in `guard_state`
  (`since`, `class`, `why`, `question`), one `set_aside` event and `level: aside` in the stall record.
  The card keeps its status. `epic_clean.holders` skips it, so its lane runs the other cards. A `todo`
  card behind it stays `todo`. `stalls --json` lists it with the evidence `waits_on_aside`. A stall
  at `aside` or `decided` sends no more pages. A human verb on the card ends the stall, and the
  aside row goes with it. A row of the quiet-stop ladder (`stall_continue`) has no `class` key and stays.
- Decide alone. After `unattended.decide_after_s` (3600) of stall age, or `decide_idle_s` (3600) when no
  run is open and no card is ready, Capsule takes the recommended option of a reversible choice. The
  source is the stored guide ruling (`ruling: ask-owner`, `provisional`, `reversible` in the stall
  record, from unit 2), or the newest `decision_requested` of a decision card (`recommended`,
  `reversible: true`). A decision with no `recommended` adds one line to the stall incident and
  stays aside. The choice writes one `auto_decision` event (`id`, `option`, `source`, `undo`) and one
  `file` outbox row with the `veto` command.
- First screen. A first-screen hold with no answer passes after `unattended.first_screen_s` (86400;
  `null` means never). The event `first_screen_ok` carries `by: capsule`.
- Veto. `python3 -m capsule veto <board> <decision id>` (the `id` of the `auto_decision` event) parks the card with the reason `vetoed <id>: ...`
  (or holds the first screen again) and writes `auto_decision_vetoed`. A second veto of one id exits 1 (`capsule/stall_veto.py`).
- Fix cards. A lane with no ready or live card for 30 min releases the oldest held Korin fix card
  whose files are free (`unblocked`, `by: capsule`), at most `unattended.fix_release_per_day` a day (unset means 0: no release; the live config sets 3).
  Only a card whose newest hold reason starts "Korin fix card for incident" is released, once per card
  since the last human verb.
- Needs you. When every open card is set aside and no run is open, one `local` row says "needs you";
  the next comes 24 h later.
- Slow page. A stall older than 24 h pages the Mac each `unattended.slow_repage_s` (21600) and Slack
  once a day. Set `slow_repage_s` to 0 to keep the 30 min and 2 h rhythm for ever.
- Silent watchers. `patrol_silent` (beat older than 90 min) and `heal_silent` (20 min) read the probes
  `patrol_beat_at` and `heal_beat_at`. With no probe or no job, no item. One kickstart per stall, L2 10 min later.

## Never list (`stall_owner.may_decide`)
Capsule never decides alone when one of these is true. The card stays aside.
- A safety finding, or the trigger `owner_rounds`.
- `block_kind` is `parked` or `spec_stale`.
- The card changed a protected path (`workspace.touches_protected`, a `protected_path_refused` event,
  or `unattended.irreversible_paths`).
- The card already got one decision since its last human verb (`intervention` event).
- The board made `unattended.decisions_per_week` (10) decisions in 7 days.
- A `pause:board`, account pause or `weekly_pause` is held.
- The source says `reversible: false`, or gives no option.

## Failure modes and what happens
`send_local` runs `/usr/bin/osascript` with a 10 s limit. A missing tool or a failed command is one stderr
line and the row counts as delivered; the next page is the retry. With `AGENT_OPS_NO_SEND=1` no command runs
and one JSON line goes to `<board dir>/local-no-send.jsonl`.

## Host guards (unit 4b)
`stall.step` calls `host_guard.step` last, also while the board is paused, so a pause can end by itself.
- Disk: free GB of the volume that holds `$HOME` is read once per pass (seam: `probes["disk_free"]`).
  Under `stall.disk_floor_gb` (15): `launchctl kickstart` of the D204 `disk-relief` job, at most once per
  30 min (seam: `probes["kickstart"]`; no launchctl under `AGENT_OPS_NO_LAUNCHCTL=1`), and guard
  `host:disk` stops new strong-tier cards in `dispatch.ready`. Under `stall.disk_pause_gb` (8):
  `pause:board` with `reason: disk` (15 min, renewed each pass). It is lifted when free space is back
  over the floor. A pause with another reason is never changed. Nothing is deleted; D204 owns that.
- `failure_storm`: the last 5 ended runs, on 3 or more cards, all `spawn_failed` or crashed (a `failed`
  run with a `crashed` event), inside 30 min, newer than the last storm pause. It pages at once (L2) and
  `host_guard.step` holds `pause:board` (`reason: failure_storm`) for 1 h, then 2, 4 and 6 h (guard
  `host:storm` keeps the level). After a pause `dispatch.ready` starts at most one card. A failed probe
  pauses at the next level. A completed run, or 24 h without a failure, resets the level.
- Log caps: `python3 -m capsule.log_caps` reports each log that `cleanup.sh` does not name (check only).
- Events: `stall_action` with class `host_disk` or `failure_storm` (`pause`, `resume`, `kickstart`, `reset`).

## Tests that pin it
Behaviour contract in the design (unit 1, items 1 to 17); the blind tests live in the separate tests repo.
`capsule/tests/test_lane_hold_alert.py` pins the status view of a held lane.

## The patrol (unit 7)
The patrol is the outer layer: a 30 min script with an optional agent call. It adds the commands
`capsule patrol` and `capsule why`, the stall class `patrol_agent_down` (`capsule/stall_patrol.py`) and
the heal check `patrol`. See [patrol](patrol.md).

## Related decisions and specs
D338 (Mac page each 30 min, Slack at the start then each 2 h), D154, row 456, row 642, Zenkai Z5.

## Status
Units 1, 2 (the L1 guide ruling), 3 (run and landing remedies; quiet stops and continue), 6 (owner-wait timers, set aside, decide alone) and 4a built 4a (`capsule/tick_watch.py`, the tick watchdog; it restarts a bad tick on the 2nd sample and reads `tick.beat`) and 4b (`capsule/host_guard.py`; `capsule tick` writes `tick.beat` once per pass, `capsule/__main__.py`) built. Unit 5 (retire the outside job) is design only.
