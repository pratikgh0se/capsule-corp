# Role: auditor (weekly board audit)

## Purpose
Audits one window (weekly, or early after 30 review rounds across the board): what got worse,
repeats, cost per delivered card, review rounds, flaky tests and stuck cards; proposes at most 3
cards.

## Where it lives (files)
- Contract: `contracts/auditor.md` (schema `auditor_report.v1`). Intent: `contracts/intent/auditor.jsonl`.
- `capsule/auditor.py`: `python3 -m capsule auditor packet --board B [--window START END] [--prior FILE] [--out FILE]` builds the deterministic, read-only audit packet (EVAL-06; default window the 7 days ending now; a source the board lacks is listed in `missing`, never counted as 0; `--prior` marks what got `worse`). It reads the board DB read-only (`db.connect_ro_tolerant`: tables `tasks`, `task_runs`, `task_events`, `incidents`) and the human spool (`spool.human_by_task`). Metrics: `landed`, `tokens`, `review_rounds`, `interventions`, `incidents`, `flaky_tests`, `stuck_cards` (no event for 48 h) and `cost_per_card`. `repeats` lists incidents with `repeat_of`. `trigger` is `rounds` when 30 or more review rounds ran since the prior window, else `weekly` (`ROUNDS_TRIGGER`).
- No runner or schedule launches the model on `main`.

## Inputs and outputs
In: an audit packet (built by `capsule auditor packet`); out, `auditor_report.v1`. A runner would file each accepted proposal as an intake request at landing `triage`.

## State it reads and writes
Packet, repo and board DB read-only; no network, no vault write, no intake write.

## Key rules and invariants
Per contract: a missing input goes in `not_audited`, never read as zero; at most 3 proposals, each citing packet evidence.

## Failure modes and what happens
Nothing launches the auditor model.

## Tests that pin it
`capsule/tests/test_auditor.py`, `test_contracts.py`, `test_intent.py` with `capsule/tests/fixtures/schemas/auditor_report.v1.valid.json`.

## Related decisions and specs
EVAL-06 (2026-10-23); rows 456, 584, 617; Zenkai Z2, Z5. See [../planned.md](../planned.md).

## Status
partial: contract, schema and the packet builder exist; nothing runs the auditor model or files its proposals.
