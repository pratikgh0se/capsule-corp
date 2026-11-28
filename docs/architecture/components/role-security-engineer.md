# Role: security engineer

## Purpose
Triages the nightly security scan findings into proposed ledger lines (fix, accept or ignore),
each with a reason and a date. It proposes; it never accepts a risk on its own.

## Where it lives (files)
- Contract: `contracts/security-engineer.md` (SEC-10, M173; schema `report.v1`). Intent examples: `contracts/intent/security-engineer.jsonl`.
- The data it reads: `python3 -m capsule security findings` (`capsule/security.py`), the security verdicts ledger of the repo under review (the path `docs/security-verdicts.jsonl` is `LEDGER` in `capsule/security.py`; this repo ships no such file) and the threat model (docs/threat-model.md, checked by `python3 -m capsule threat-model check`, `capsule/threat_model.py`). No runtime code launches this role. See [role-security-reviewer](role-security-reviewer.md) for the diff-time reviewer, a different role.

## Inputs and outputs
In: the untriaged finding ids, the verdict ledger and the threat model. Out: `report.v1` with the proposed ledger lines, the findings left untriaged and why, and the `security check` totals line.

## State it reads and writes
Read-only on the repo, findings and ledger; no network, no secrets. It writes nothing outside its report.

## Key rules and invariants
- Every untriaged finding id gets one line with a verdict, a non-empty reason and a date.
- An accept names a decision (`row N` or `DN`) from the threat model, or is left as a question for the owner.
- No secret value is quoted. It may not edit code, a contract, a schema or a test, and may not close an accepted risk without a decision.
- Zenkai Z2 (the ledger check is the second oracle) and Z4 (no bypass without a named override).

## Failure modes and what happens
Nothing launches the role today, so there is no run to fail. By contract, findings it cannot triage stay open in the report. See [../planned.md](../planned.md) (security pack).

## Tests that pin it
`capsule/tests/test_contracts.py`, `test_intent.py`.

## Related decisions and specs
SEC-10, M173; [zenkai](zenkai.md).

## Status
planned (contract only): `capsule security` and `capsule threat-model` exist; nothing launches the role.
