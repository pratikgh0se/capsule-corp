# Vault context for workers

## Purpose
Put the decision rows and notes a card needs into a worker's brief, pushed by dispatch, so no role
mounts the vault. Advisory only (Z3): a stale or contradicted note is withheld, and the worker is
never blocked on it.

## Where it lives (files)
- `capsule/vault_context.py`: `retrieve` (a card's decision rows and notes), `guard` (withhold the stale and contradicted), `render` and `attach` (into the brief). The tick calls `attach` at dispatch (`capsule/tick.py`, row 626: the reason is that the worker needs the context before it starts); `capsule/worker.py` only passes the section into the brief. `retrieve` also serves `capsule vault-context` and `capsule/brainstorm_run.py`.
- `capsule/vault_context_cli.py`: `capsule vault-context pending|verdict`, the CoS review queue of flagged retrievals.
- `capsule/vault_eval.py` (ships); bin/vault-eval and bin/vault-search (maintainer only, not in the public edition): the retrieval eval set and gate (MEM-13), with Capsule's own oracle that shares no code with the vault skill's eval (Z2).
- Config: `redesign.vault_context` = `off` | `report` | `live`; route key `vault_read` (`none`, `decisions`, `notes`) per role (`capsule/config.py` `VAULT_READ`).

## Inputs and outputs
In: the card text and the vault (read-only, by dispatch). Out: a brief section (live only); `<spool>/vault_context.json` and one `vault_context` event per attach in `report` and `live` modes; `vault_context_verdict` events and eval verdict lines from the CoS review queue.

## State it reads and writes
Reads the vault; writes only events and the eval verdicts file.

## Key rules and invariants
`off` changes nothing. `report` computes and records but does not attach. `live` attaches. A role with no `vault_read` row reads nothing. Every value arrives by dispatch push; no role mounts the vault.

## Failure modes and what happens
Vault unreadable or search down: no section, the run proceeds.

## Tests that pin it
`capsule/tests/test_vault_context.py`, `test_vault_context_cli.py`, `test_vault_context_guard.py`, `test_vault_read_routes.py`; `contracts/intent/vault_context.jsonl`, `vault_read_routes.jsonl`.

## Related decisions and specs
Row 626, row 128; spec "Vault context for workers" (6, 7.1, 7.2, 7.4).

## Status
current, opt-in per board.
