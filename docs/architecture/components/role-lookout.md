# Role: lookout (observability coverage reviewer)

Not to be confused with the Lookout data feed (spool and shipper), documented in
[observability](observability.md).

## Purpose
Reviews one application's repo, specs and observability docs and proposes missing signals,
dashboards, alerts and SLOs as a decision card.

## Where it lives (files)
- Contract: `contracts/lookout.md` (schema `lookout_proposal.v1`). Intent: `contracts/intent/lookout.jsonl`.
- `capsule/tests/test_lookout_role.py` pins the role's contract. No runtime code launches it.

## Inputs and outputs
Contract only: in, an app repo, specs, rows and the app repo's own observability notes (this repo's shared rules are in `docs/observability-conventions.md`); out, `lookout_proposal.v1` (coverage, proposals, questions, confidence).

## State it reads and writes
Read-only: app repo, `observability/`, vault. No Grafana credentials, no network.

## Key rules and invariants
Per contract: may not apply a proposal, file a card or decision, edit a dashboard or alert, or silence an alert. Scripts apply what the maintainer approves.

## Failure modes and what happens
Nothing launches it.

## Tests that pin it
`capsule/tests/test_lookout_role.py`, `test_contracts.py`, `test_intent.py`.

## Related decisions and specs
TEAM-14; rows 21, 64; app monitoring and incident widening (planned, overnight queue F7): see [../planned.md](../planned.md).

## Status
planned (contract only).
