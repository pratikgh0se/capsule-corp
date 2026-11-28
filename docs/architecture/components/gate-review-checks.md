# Gate: in-tick review checks

In short:

- **What it is.** These are the checks the tick runs on the review documents and on the change.
- **What it does.** Code checks what the reviewer and validator wrote, so a model that misreads the evidence cannot pass a card.
- **Where the code is.** `capsule/tick_review.py` and `capsule/review_findings.py`.

## Purpose
After the implementer reports, the tick decides the round. Review roles judge, but code (a second
oracle) checks their documents and the change itself, so a model misreading evidence cannot pass a
card.

## Where it lives (files)
- `capsule/tick_review.py` (`TickReview` mixin): `_review`, `_review_one`, `_first_screen`,
  `_open_findings`. `capsule/tick.py`: `_start_reviews`, `_verdict`, `_mark_overrides`.
- `capsule/review_findings.py` (a pass with findings is invalid), `capsule/validator_report.py` (no `not_run` with outputs present), `capsule/check_gate.py` (card `check_cmd`), `capsule/review_scope.py` (out-of-set findings), `capsule/guide_notes.py` (guide `amendment` and `notes` scope), `capsule/style_lint.py` (output style), `capsule/invalid_doc.py`, `capsule/override.py` (reasoned, TTL'd, per-check bypass), `capsule/epic_sync.py` (`finish_gate_pass`), `capsule/protected_scope.py` (what the protected files should be when the host moved the lane; it also words the `protected path changed (row 395)` failure), `capsule/security_review.py` (the optional fourth review), `capsule/blind/gate.py` (blind rows, gate mode), `capsule/claims.py` and `capsule/diff_ceiling.py` (pre-flight and landing-size checks).

## Inputs and outputs
In: the latest document of each review role, the clone's changed paths against the round's start sha, the validator's contract outputs. Out: pass (card to `done` on the epic branch with a `completed` event carrying the commit and gate words), a failed round (comment plus `record_failure` back to `ready`), or a block (`needs_input`, `spec_stale`, `review_infra`).

## State it reads and writes
Board tables; the clone (a planted protected file is restored to the start sha).

## Key rules and invariants
- A round fails on: a changed protected path (row 395); content findings from reviewer or validator; a failing `check_cmd`. Failures are listed together in one comment.
- A review role with no valid document after its retry is review infra, never a card failure (`review_infra` block, cause recorded).
- The guide's `drifted` blocks the card `needs_input` (unless the guide had no citation: `aligned_uncited`); `spec_conflict_rows` gives `spec_stale`.
- An override suppresses exactly the named check, expires at its TTL, requires a fix card and issuer, and is itself an incident (Z4).
- Style: `style_lint` is advisory for sentence, step and paragraph length; schema-pinned fields are blocking.
- Stop rules (`restart.stop`, `runaway`) run before a fail returns the card to `ready`.

## Failure modes and what happens
See [blocks-and-stop-the-line](blocks-and-stop-the-line.md). A pass with findings: invalid document, retried once with the refusal message. So is a `fail` whose finding text offers a second fix (`review_findings.TWO_FIXES_MARKERS`); the reviewer prompt says one fix per finding.

## Tests that pin it
`capsule/tests/test_review_findings.py`, `test_validator_report.py`, `test_check_gate.py`, `test_review_scope.py`, `test_guide_notes.py`, `test_style_lint.py`, `test_override.py`, `test_invalid_doc.py`, `test_epic_sync.py`, `test_tick.py`.

## Related decisions and specs
Rows 362, 363, 395, 447, 454 item 4, 507, 509; spec section 6.3 to 6.4.

- `capsule/contract_audit.py` (D220): the reviewer audits each numbered acceptance row in the optional `contract_audit` array of `review_verdict.v1`; soft check, adds notes and never refuses a verdict.

## Stall remedies
A `review_infra` block gets one automatic re-run by the stall supervisor: `blocks.unblock(by='capsule')`
returns the card to `review` on its latest implementer run, so only the review roles run again. A review
run row with no worker pid for 5 min ends `spawn_failed` (the review pass starts a new run), and a card
waiting for a review seat 30 min is a `review_deferred` stall that lists the seat holders (see
[stall-supervisor](stall-supervisor.md)).

## Status
current.
