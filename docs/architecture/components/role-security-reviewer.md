# Role: security reviewer

## Purpose
Reads the diff and manifest changes for security issues: new network paths, secret paths and
dependency paths, with a severity per finding. A fourth blocking review, behind a switch.

## Where it lives (files)
- Contract: `contracts/security-reviewer.md` (schema `review_verdict.v1`, `role: security`). Intent: `contracts/intent/security-reviewer.jsonl`. A second contract, `contracts/security-engineer.md`, exists for the security engineer role.
- `capsule/security_review.py`: `ROLE = "security"`, `roles(cfg)` (the board's review round), the manifest matcher (dependency, lock, image and CI files) and the diff packet (each of the two diffs capped at `MAX_PART`).
- `capsule/tick_review.py`: `REVIEW_ROLES` is `security_review.BASE` (validator, reviewer, guide) plus `security`; `security_review.roles(cfg)` decides per board. `capsule/tick.py` starts the run.

## Inputs and outputs
In: the diff, its manifest part and the card's risk class (row 366), never another review's verdict. Out: `review_verdict.v1` with `role: security`.

## State it reads and writes
Host run, read-only, no network, may not run the code under review.

## Key rules and invariants
- Off unless `redesign.security_review` is on; config validation then requires `routing.security` (BLD-02).
- Every verdict carries `role: security`; a pass carries zero findings; it must name every new network, secret and dependency path or state none.
- It joins the validator, reviewer and guide as a blocking review when on.

## Failure modes and what happens
Same as the other review roles: no valid document after the retry is review infra, never a card failure ([gate-review-checks](gate-review-checks.md)).

## Tests that pin it
`capsule/tests/test_security_review.py`, `test_contracts.py`, `test_intent.py`, `test_vault_read_routes.py`.

## Related decisions and specs
Rows 362, 366, 381; BLD-02; spec Appendix A.6. Scan verdicts and the threat-model check (`capsule/security.py`, `capsule/threat_model.py`, M173) are separate and live under [maintenance-jobs](maintenance-jobs.md).

## Status
current, opt-in per board (`redesign.security_review`).
