# Gate: test-tamper guard

## Purpose
Stop a worker from making a failing gate pass by deleting, skipping or loosening the tests that
judge it. The documented failure is an agent deleting the assertion, then the file.

## Where it lives (files)
Protection is layered across several pieces:
- `contracts/implementer.md` and `contracts/reviewer.md`: the implementer may never delete, weaken or move a guard test (row 568); a weakened or deleted test is a reviewer finding.
- `tests/mutation_check.py`: assertion-weakening mutation and static skip flags on changed tests ([gate-mutation-check](gate-mutation-check.md)).
- `capsule/workspace.py` `PROTECTED`: the judge files themselves cannot be edited ([gate-protected-paths](gate-protected-paths.md)).
The earlier diff-level tamper check (four diff signals over `tests/`) was removed 2026-11-13; `OPS_TEST_TAMPER` is no longer seeded.

## Inputs and outputs
A gate table (mutation check).

## State it reads and writes
None.

## Key rules and invariants
A test that must change is a block, raised to the maintainer, not a rewrite.

## Failure modes and what happens
Red mutation gate or reviewer finding.

## Tests that pin it
`tests/test_mutation_check.py`, `capsule/tests/test_contracts.py`.

## Related decisions and specs
Row 124, 129, 568.

## Status
partial. The lander's default gates do not include a diff-level tamper check.
