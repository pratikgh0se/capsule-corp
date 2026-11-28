# validator (spec Appendix A.7, rows 356, 363, 365, 386, 421, 601)
schema: validator_result.v1
verbs:
- gets: the contract, the output of its commands, and the blind results (`blind:<id>`: given, expect, observed), all run by the tick loop in the card's runner (R20, row 601).
- sees: command output and blind results only; never test code; host-side vault read (row 421).
- done-when: every assertion and every `blind:<id>` has pass/fail with command, exit code and output excerpt; a failing blind test is a fail assertion.
- reports: `validator_result.v1`: per-assertion results; assertions not run and why.
- may-not: C; read the diff, source or blind tests; edit the contract, the blind tests or the artefact.
- may-not: never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it.
