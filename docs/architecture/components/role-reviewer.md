# Role: reviewer

## Purpose
Reads the diff against the contract and spec and returns pass or findings. A code-reading judge,
independent of the validator.

## Where it lives (files)
- Contract: `contracts/reviewer.md` (schema `review_verdict.v1`, verb: comment). Intent: `contracts/intent/reviewer.jsonl`; `contracts/shown/reviewer.jsonl`.
- Bundle: `bundles.reviewer`. Start: `Tick._start_reviews` in `capsule/tick.py`; `REVIEW_ROLES` and `REVIEW_TRIES` live in `capsule/tick_review.py`. Verdict handling: `Tick._verdict`, `capsule/review_findings.py`, `capsule/review_scope.py`.
- Host runner: `capsule/runners/host.py`.

## Inputs and outputs
In: the diff, contract, spec section, round number. Out: `review_verdict.v1` with `verdict` (pass or fail), `findings` (file:line and class: slop, design_fidelity, least_code, more_than_needed, out_of_set), `notes`, `could_delete` and the five Z8 `simplicity` answers.

## State it reads and writes
Reads the clone read-only on the host. Writes only its spool document; the tick stores it as the run's `handoff`.

## Key rules and invariants
Route default Opus, 40 turns, `vault_read: decisions`. A `pass` with non-empty `findings` (row 362), or a late non-safety `fail` (row 540), is refused before the intent check and retried once as an invalid document (`review_findings.py`; a late fail means round 4 or later, `LATE_ROUND`). Each review must answer "what could be deleted" (Z7) and the Z8 questions. A weakened or deleted test is a finding. The reviewer bundle also carries the advisory hollow-test lines (`capsule/hollow_tests.py`, D85, warn-only) for changed Python, Swift and web tests. A guide `document-wrong` ruling on a reviewer verdict reruns the reviewer (`capsule/reviewer_rerun.py`). A review role gets at most 2 tries per round (`REVIEW_TRIES`).

## Failure modes and what happens
No valid document after the retry: `review_infra` hold (`accept` re-runs the round; the card is not charged). A pass contradicting an intent example: contradiction routed to the guide for a ruling.

## Tests that pin it
`capsule/tests/test_review_findings.py`, `test_review_scope.py`, `test_intent.py`, `test_contracts.py`, `test_tick.py`; `contracts/intent/reviewer.jsonl`, `review_gate.jsonl`.

## Related decisions and specs
Spec Appendix A.5; rows 362, 363, 365, 509, 568, 601.

## Status
current.
