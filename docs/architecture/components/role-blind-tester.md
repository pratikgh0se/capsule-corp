# Role: blind tester

## Purpose
Writes tests from the acceptance text and an interface snapshot only, never seeing the diff or
source ("hidden tests" for the implementer). The aim is a judge the implementer cannot have shaped.

## Where it lives (files)
- Contract: `contracts/blind-tester.md` (schema `blind_tests.v1`; result schema `blind_result.v1`). Intent: `contracts/intent/blind_tester.jsonl`, `blind_runner.jsonl`, `blind_wall.jsonl`.
- Dispatch and close: `capsule/blind/dispatch.py`, `launch.py`, `close.py`, `brief.py`, `snapshot.py`. Routing key: `blind-tester` in `routing` (optional; without it the tick dispatches no tester and says so, `config.BLIND_TESTER`).
- Config switch: `redesign.blind_tests` (`off`, `report`, `gate`).

## Inputs and outputs
In: the card, `card_hash`, interface snapshot, harness API. Out: tests under `cards/<id>/**` in the blind repo and a `blind_tests.v1` report that covers or explains every numbered acceptance statement.

## State it reads and writes
A host-side fresh clone of the blind repo on branch `card/<id>` at `/workspace` (read-write), the snapshot at `/interface:ro`, `/deps:ro`. No project worktree, no prior spools. Host code, not the tester, lands the tests on the blind repo's `main` after lint and the red check.

## Key rules and invariants
Sees acceptance text and the interface snapshot only; may not read outside `/workspace` and `/interface`; may not weaken or delete an existing test. The run is review-shaped (no lease, no card ownership, one review seat, read-only host tier). A change to a landed blind test is a risky card (D18).

## Failure modes and what happens
A tester run that fails or times out is retried; `MAX_TRIES = 2` runs per card hash (`capsule/blind/dispatch.py`). A runner failure in the red check is retried on a later pass and the third triages the card (`capsule/blind/red.py`). A tester round that does not land is bounced to the tester with outcomes only; the second bounce triages the card.

Reads: the file tools read only the blind clone (no `add_dirs`). See [read-guard](read-guard.md).

## Tests that pin it
`capsule/tests/test_blind_run.py`, `test_blind_gate.py`, `test_blind_harness.py`, `test_blind_red.py`, `test_blind_dispatch.py`, `test_blind_text.py`, `test_blind_reconcile.py`, `test_contracts.py`, `test_intent.py`, `test_schema_contracts.py`.

## Related decisions and specs
Rows 601, 602; [blind-path](blind-path.md). Pipeline: [blind-path](blind-path.md).

## Status
current, opt-in per board.
