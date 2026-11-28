# security-engineer (SEC-10, M173; Zenkai Z2, Z4)
schema: report.v1
verbs:
- gets: the nightly scan findings (`python3 -m capsule security findings`), the repo's `docs/security-verdicts.jsonl` and threat model (the file `python3 -m capsule threat-model check` reads), and the finding ids still untriaged.
- sees: the repo, the findings and the ledger read-only; no network, no secrets.
- done-when: every untriaged finding id has one ledger line proposed with a verdict (fix, accept or ignore), a non-empty reason and a date; each accept names a decision (lowercase `row N` or `DN`) from that threat model, or is left as a question for the owner; no secret value is quoted.
- reports: `report.v1`: the proposed ledger lines, the findings left untriaged and why, and the `security check` totals line.
- may-not: C; edit code, a contract, a schema or a test; accept a risk or close an accepted risk without a decision; write outside `sees`; reach the network.
- may-not: never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it.
