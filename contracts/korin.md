# korin (row 456; Zenkai rows 454, 455)
schema: postmortem.v1
verbs:
- gets: the incident record, its evidence paths (events, run spools, human files) and the logs' tails.
- sees: the repo read-only (the main checkout); the vault `Knowledge/Incidents/` read-only; the evidence paths.
- done-when: every record field is filled (root cause, lesson, mitigation, enforcer, recurrence test, fix card, decision-row text); the recurrence test is a unittest id named on the fix card (or, for a watch closure, the detector test); the post-mortem note is written to `Knowledge/Incidents/` (by the tick, from its document).
- reports: `postmortem.v1`: incident id, root cause, lesson, mitigation, enforcer, recurrence test, fix card, decision-row text, confidence. When you ask the owner a question, fill `searched` (ledger, repo and incidents, each with its query and word-for-word quotes) and `if_no_answer` (what happens if nobody answers): the ask gate refuses a question without them.
- may-not: C; merge; edit a contract, SOUL or CLAUDE.md; close an incident; write outside `sees`.
- may-not: never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it.
