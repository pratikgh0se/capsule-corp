# Zenkai rules (reliability doctrine)

The rules live in [../../ZENKAI.md](../../ZENKAI.md), a checksummed verbatim mirror of the vault note
`Knowledge/Capsule Corp - Zenkai Rules.md` (decision rows 454 and 455). Cite them by item (Z1 to
Z8). This page says how the repo enforces them and never restates them.

## Purpose
Reliability doctrine that binds every agent and every check: intent as a separate artefact, two
oracles, precedence, expiring exceptions, always-on detectors, autonomy by reversibility, less code,
simplicity.

## Where it lives (files)
- `docs/ZENKAI.md` (protected: `workspace.PROTECTED`), `capsule/tests/test_zenkai_mirror.py` (fails on drift from the mirror checksum).
- `docs/briefs/zenkai-brief-block.md`: the eight lines pasted into every implementer and reviewer dispatch.
- Every role contract ends with a may-not line naming the Zenkai Rules (`contracts/*.md`).
- `CLAUDE.md` (repo) states who applies which item.

## Inputs and outputs
Not a runtime component. Inputs are code changes; outputs are failed gates and reviewer findings.

## State it reads and writes
None.

## Key rules and invariants (where each item is enforced)
- Z1 intent examples: `contracts/intent/*.jsonl`, `capsule/intent.py` ([gate-intent-contracts](gate-intent-contracts.md)).
- Z2 two oracles: every check ships with a second, independent oracle (for example `tests/size_check.py` with ruff; `capsule/eval_check.py`; mutation gate fixtures).
- Z3 precedence and Z4 overrides: `capsule/override.py`, override incidents ([incidents-korin-flow](incidents-korin-flow.md)).
- Z5 detectors: `capsule/idle_lane.py`, `capsule/invariants.py`, `capsule/metrics.py`, the canary slice after every merge (task t_c9cf359b).
- Z6 autonomy by reversibility: contracts and the CoS stamp rules.
- Z7 and Z8: `tests/size_check.py`, `tests/simplicity_check.py` ([gate-simplicity-and-size](gate-simplicity-and-size.md)).

## Failure modes and what happens
A drifted mirror fails `test_zenkai_mirror`. A worker editing `docs/ZENKAI.md` is a protected-path hit.

## Tests that pin it
`capsule/tests/test_zenkai_mirror.py`, `capsule/tests/test_contracts.py` (may-not line present).

## Related decisions and specs
Rows 454, 455.

## Status
current.
