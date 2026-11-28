# Gate: hidden (blind) tests

## Purpose
Tests the implementer never sees, written from the acceptance text only, so the implementer cannot
write code that merely satisfies its own tests. Their results reach the validator as `blind:<id>`
evidence and, in gate mode, decide whether a card lands. The whole path is described in
[blind-path](blind-path.md); this page covers the gate itself.

## Where it lives (files)
- `capsule/blind/run.py` (the blind run and its rows), `capsule/blind/gate.py` (hold, release, cap, failure text), `capsule/lander_blind.py` (the lander's re-run on the exact commit), `capsule/blind/red.py` (red check before tests land).
- `capsule/config.py`: `redesign.blind_tests` = `off` | `report` | `gate`; the board's `blind_tests` block (`repo`, `adapter`, `tester_may_read`, `interface_commands`, `also_walled`).
- `capsule/tick.py` and `capsule/blocks.py` (`BLIND_CAP_LABEL`): the tick reads the rows from the validator run's own `job.json` and holds or fails the card.
- Protected: `capsule/blind/harness/**` and the judge files listed in `workspace.PROTECTED`.

## Inputs and outputs
In: the blind tests landed for the card (`tasks.blind_commit`), the card's work. Out: `blind:<id>` rows (`ok`, `fail`, `hold`), `blind_gated` events, holds with a plain reason, a `decision.v1` at the cap.

## State it reads and writes
Reads the blind repo through a `git archive` snapshot at the recorded `blind_commit`; writes board events and holds only.

## Key rules and invariants
- `off` changes nothing. `report`: rows never fail or block a card. `gate`: a failing test fails the round; a missing, stale, unavailable, inconclusive or fully skipped result holds the card (never a silent pass).
- The hold offers `accept` or `reset`. An `accept` from the owner (author `owner`, self-declared with `--author`; the CoS author waives nothing) naming an open fix card waives it for the card's current acceptance text and hold reason, for one implementer run and 72 hours (Z4; one `override` event).
- The second verifier round that still has failing blind tests holds the card with one decision for the owner instead of a third round (M110: a third round would repeat the same failure).
- The lander runs the blind tests again at the recorded commit against the commit it will push; a card with no blind tests that would get a tester is held `no blind tests yet`.

## Failure modes and what happens
See [blind-path](blind-path.md). An inconclusive run (oracles disagree, worktree changed) opens a `review_infra` incident.

## Tests that pin it
`capsule/tests/test_blind_gate.py`, `test_blind_gate_cap.py`, `test_blind_gate_hold.py`, `test_blind_gate_lint.py`, `test_blind_gate_review_round.py`, `test_blind_gate_tick.py`, `test_blind_run.py`, `test_blind_run_parts.py`, `test_blind_run_tick.py`, `test_lander_blind.py`, `test_lander_blind_closed.py`, `test_lander_blind_holds.py`, `test_lander_blind_keeper.py`, `test_lander_blind_release.py`; `contracts/intent/blind_runner.jsonl`.

## Related decisions and specs
Rows 601, 602; [blind-path](blind-path.md). The hollow-test detector (`capsule/hollow_tests.py`, D85, warn-only) covers Swift and web tests the Python mutation check cannot.

## Status
current, opt-in per board.
