# Blocks, stop-the-line and the restart ladder

## Purpose
Turn every way a card can stall into a typed hold with an owner and a way out, and stop a card
that keeps failing instead of retrying forever.

## Where it lives (files)
- `capsule/blocks.py`: `block` (router and failure gate), park, epic reassign, first-screen hold.
- `capsule/runaway.py`: stop-the-line on rounds, runs, tokens and the same finding class on the same file.
- `capsule/stall.py`: a card in triage or blocked (any `block_kind`, from the newest `blocks.HOLD_EVENTS` event) that nobody lifts for 5 min is a stall (`held_triage`, `held_blocked`; a park is `held_parked`; a hold only the owner can lift is `owner_wait`). It reports the hold and never lifts it. See [stall-supervisor](stall-supervisor.md).
- `capsule/restart.py`: the Sonnet-then-Opus ladder (`redesign.escalation`).
- `capsule/contradiction.py`, `capsule/invalid_doc.py`, `capsule/review_scope.py`, `capsule/ruling_sweep.py`: contradiction routing, invalid-document handling, out-of-set findings, ruling-card sweep.
- `capsule/amend.py`, `capsule/autoamend.py`: operator amend verb; tick-applied scope-neutral fix proposed by the guide (`autonomy.auto_amend`).
- `capsule/cost.py`: card cost, used by runaway limits.
- `capsule/claims.py`: pre-flight claim check (D84): before a card's first dispatch, repo paths named in backticks must exist at the clone's HEAD (git only); a card naming a missing path goes to the guide on the runaway pattern. A `File set:` naming a protected path parks (row 395). Warn-safe: unparseable input never blocks.

## Inputs and outputs
In: a failed run, a review verdict, a contradiction, a human verb. Out: a status change plus an event and one outbox line on the card's thread.

## State it reads and writes
`tasks.block_kind`, `block_recurrences`, `task_events`, `task_runs`; `guard_state`.

## Key rules and invariants
- Block kinds (`VALID_BLOCK_KINDS`): `dependency`, `needs_input`, `capability`, `transient`, `review_infra`, `contradiction_loop`, `spec_stale`, `invalid_document`, `epic_sync_conflict`, `epic_sync_error`, `owner_look`.
- The same kind twice (`BLOCK_RECURRENCE_LIMIT = 2`) moves the card to `triage` and logs `block_loop_detected`; a `dependency` block goes to `todo` and waits.
- `capsule human <board> <task> wait <ref>` (`blocks.wait`) holds a card on a declared epic branch (a person's `reset` lifts it) or a commit sha. For a sha, `main_waits.release` (every tick, before `promote_ready`; it reads the local `repo_url` in place, no fetch) lifts it with a `main_wait_released` event once the commit is in main; a git error or timeout keeps it waiting, an unknown sha or a URL `repo_url` notifies once.
- Runaway defaults (`runaway.DEFAULTS`): 4 rounds, runs and tokens at 3 times the trailing median of landed cards (the runs count warns at 2 times; tokens have no warn step), 3 rounds of the same finding class on the same file (D335), floors of 8 runs and 150000 tokens, minimum 5 landed cards for the median; `owner_rounds` 5, the round that goes straight to the owner with no guide pass (row 642). With the ladder on, the `max_rounds`, `owner_rounds` and `same_class_rounds` limits are raised to at least the ladder length (`restart.LADDER`, 6), so the Sonnet streak cannot stop the card before the hand-off.
- Ladder (`restart.py`): 3 failed review rounds on the Sonnet route (attempt plus 2 fixes), then one fresh run on `routing.implementer_restart` with its failure notes and 2 fix rounds, then triage. Off unless `redesign.escalation` and the route exists; with it off, behaviour is the plain failure cap.
- Auto-amend applies only scope-neutral hunks within `autonomy.amend_max_lines` (max 12), to targets in `amend_targets`.

- A stall ruling release is `by='capsule'` (`blocks.unblock`, `blocks.reset_failures`). Only `by='human'` refills the runaway budget, so the card gets exactly one more run after a stall release.

## Set aside and veto (stall supervisor, unit 6)
A card set aside by `capsule/stall_owner.py` keeps its status and its hold. It holds no lane. After
`unattended.decide_after_s`, a reversible choice with a recommended option is applied with `by: capsule`
(`failure_reset` or `unblocked`). `python3 -m capsule veto <board> <decision id>` takes the decision back and parks the card.
A safety finding, `owner_rounds`, `parked`, `spec_stale`, a card that changed a protected path, and any active pause are never decided alone (`stall_owner.never`); so is a choice that is not reversible, and at most `unattended.decisions_per_week` (10) are made a week. See
[stall-supervisor](stall-supervisor.md).

## Failure modes and what happens
A card past its limit moves to `triage` with a ruling request for the guide; the human or guide chooses continue, restart or stop. A loop of document-wrong resets blocks after `RULING_RESETS = 2`.

## Tests that pin it
`capsule/tests/test_blocks.py`, `test_runaway.py`, `test_restart.py`, `test_restart_intent.py`, `test_invalid_doc.py`,
`test_review_scope.py`, `test_ruling_sweep.py`, `test_autoamend.py`, `test_amend.py`, `test_cost.py`; `contracts/intent/auto_amend.jsonl`, `restart_ladder.jsonl`.

## Related decisions and specs
Spec 6.4 of the Capsule 2 design; rows 359, 376, 447, 507, 509; `2026-09-27-factory-autonomy-design.md` (auto-amend, spec 2).

## Status
current.
