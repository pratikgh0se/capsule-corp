# Role: staff architect

## Purpose
Writes a short design brief for one card before the build starts, so the implementer works from a
named approach and the owner sees whether the change reaches them.

## Where it lives (files)
- Contract: `contracts/staff-architect.md` (TEAM-23; schema `report.v1`). Intent examples: `contracts/intent/staff-architect.jsonl`.
- Output file: `docs/design/<card-id>.md`, committed on the epic branch. The dispatcher reads it from the epic clone, never the main checkout.
- The yes/no in its last section matches `python3 -m capsule design reaches-owner --files N [--risk FLAGS] --reversible yes|no` (`capsule/design_briefs.py`): yes when the change is not reversible, the card has a `public` or `data` flag, or it touches more than 20 files (`BIG_FILES`).
- No runtime code launches this role. The only code is the dispatch gate in `capsule/design_briefs.py` (`missing`, used by `capsule/dispatch.py`).

## Inputs and outputs
In: one card (title, body, file set, `risk_flags`) and the repo read-only. Out: the brief with `## Problem`, `## Approach`, `## Risks` and `## Reaches the owner`, plus a `report.v1` (status, one-line summary, the brief's path).

## State it reads and writes
Reads the epic clone. Writes only `docs/design/<card-id>.md`. No network, board or vault write.

## Key rules and invariants
- Approach names each file it changes and how each is checked; Risks names every risk flag on the card and what undoes the change.
- Reaches the owner says yes or no with one reason.
- It may not edit code, a test, a contract or a schema, and may not start the build.
- Zenkai Z4 (no bypass without a named override) and Z6 (autonomy by reversibility).

## Failure modes and what happens
When the board sets `design_briefs: true`, a card with risk flags waits in `ready` until `docs/design/<card-id>.md` exists in its epic clone; the dispatcher writes one `design_brief_missing` event. The gate checks that the file exists, not that its sections are complete. Nothing launches the role, so a person or the CoS writes the brief.

## Tests that pin it
`capsule/tests/test_contracts.py`, `test_intent.py`.

## Related decisions and specs
TEAM-23; [zenkai](zenkai.md); [dispatch-and-caps](dispatch-and-caps.md).

## Status
partial: the contract and the dispatch gate exist; nothing launches the role.
