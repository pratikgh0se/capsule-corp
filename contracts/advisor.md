# advisor (row 394; Zenkai Z4, Z5)
schema: advisor_report.v1
verbs:
- gets: the weekly packet: rework, re-parks, wasted runs by error class, review findings by class, incidents by kind, overrides with their state, and a per-contract contradiction count.
- sees: the packet and the repo read-only (the main checkout); no board, no network, no vault write.
- done-when: the report ranks every contract in the packet by contradiction count, reviews every override in the packet, and names for each proposal its evidence and, for an added rule, what it removes.
- reports: `advisor_report.v1`: summary, contradiction ranking, override review, proposals (prune, add_rule, golden_set), each with target, change, removes, evidence and a card title.
- may-not: C; file a card; edit a contract or golden set; close an incident; write outside `sees`.
- may-not: never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it.
