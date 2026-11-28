# Intake and human verbs

## Purpose
Every way work or a human decision enters a board: new cards, cross-board card files, and the
`capsule human` verbs that change a card. The tick is the only writer, so all of these are files
the tick ingests.

## Where it lives (files)
- `capsule/intake.py`, `capsule/cli_intake.py`: `python3 -m capsule intake <board> ...` and the cross-board drop under `boards/<board>/intake/`; ingested in tick step 1b.
- `capsule/human.py`, `capsule/interventions.py`, `capsule/__main__.py` (`human`, `human-apply`): one JSON document per intervention under `boards/<board>/human/`; outcome stamping.
- `capsule/amend.py`, `capsule/assign.py`, `capsule/card_fields.py` (verbs `amend`, `assign`, `check`, `size`, `depends`, `workload`), `capsule/override.py` and `capsule/identity.py` (the `override` verb).
- `capsule/stop_queue.py` (verb `stop '<trigger>'`): parks a doomed card once its run ends, or
  queues `park_queued`; a stop on an approved/landing (`done`) card is refused ("approved, landing:
  use close/revert"), on an archived card "nothing to stop".
- `capsule/prior_verdicts.py`, `capsule/cli_prior.py`: `## Prior verdicts` block for a hand-cut card.
- `capsule/cli_names.py`: `names backfill` card numbering.
- `capsule/touch.py`: which human touches count as defects (a touch file may open a card on another board when `autonomy.touch_cards` is on).
- `bin/new-repo.sh` (repo creation), `bin/brainstorm-handoff.py` (handoff block for a brainstorm session), bin/approve-spec.sh (maintainer only, not in the public edition) and bin/approve-release.sh (maintainer only, not in the public edition) (write the spec and release approval stamps), bin/answer-decision.sh (maintainer only, not in the public edition) (decision replies): narrow single-purpose scripts; see Status.

## Inputs and outputs
In: operator CLI calls. Out: JSON files; the tick turns them into status changes and `intervention` events.

## State it reads and writes
Writes files under `boards/<board>/human/` and `intake/`; reads the source board read-only to prove a touch or auto-amend file names something real (provenance, a second oracle).

## Key rules and invariants
- `human.write` validates `author` (`owner` or `cos`; the old legacy author name and the configured owner name read as `owner`, and `--author owner` is accepted only from a person's own terminal), tags (`migration-reset`, `handover-route`), and the four fields an `override` needs (`check`, `ttl`, `fix_card`, `issuer`) plus a non-empty reason. A `park` or `stop` needs a non-empty trigger; a `close` or `archive` needs a non-empty reason.
- An `override` is a reasoned, TTL'd, per-check bypass of one review-stage check; it is itself an incident (Z4).
- `capsule human-apply --board <b>` applies queued interventions through the tick's own path under the tick lock, with no dispatch; it refuses a mismatched board or a held lock (exit 3).
- A `follow_up` intake card inherits its source card on the same board (`intake._inherit`): tier, assignee, `check_cmd`, `check_expect`. If the source branch is a declared epic and the assignee is an implementer, the card starts `ready` on that branch. Otherwise it stays in triage. If the source card is gone or has no tier, the old triage landing applies.
- Each intervention is estimated in minutes by action type for the human-time metric (`human_minutes.py`).

## Failure modes and what happens
A malformed file fails at the writer, not silently at the tick. A human file naming no task on this board, or unreadable, is archived to `human/done/` with one stderr line and no event; the file name is the idempotency key, so a file already recorded is archived again without a second event (`Tick._interventions`).

## Tests that pin it
`capsule/tests/test_intake.py`, `test_human_apply.py`, `test_human_author.py`, `test_interventions.py`, `test_intervention_schema.py`, `test_override.py`, `test_identity.py`,
`test_amend.py`, `test_assign.py`, `test_touch.py`, `test_prior_verdicts.py`, `test_cli_names.py`, `test_brainstorm_handoff.py`; `tests/test_new_repo.sh`.

## Related decisions and specs
Factory-autonomy design section 3.5; rows 420, 454 (item 4), 509; this page.

## Status
current. The `capsule` verbs are the operator surface. The `bin/*approve*` and `answer-decision.sh` scripts are narrow stamp and reply writers for the earlier chat-bot flow and are no longer called by the tick. There are no PR-approval, brainstorm open/close or card-repair scripts; PR approval is the owner's GitHub merge of the epic PR (D238).
