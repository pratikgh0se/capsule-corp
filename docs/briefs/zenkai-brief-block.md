# Zenkai brief block (rows 454, 455)

The controller pastes the eight lines below, unchanged, into every implementer and task-reviewer
dispatch (the SDD skill's prompt templates are plugin files and are not edited). The block cites
`docs/ZENKAI.md` by item; it never copies the rules. Change it only with a new ZENKAI version.

```text
ZENKAI: docs/ZENKAI.md (Zenkai Rules v2) binds this task; read Z1-Z8 there before you start; never copy it.
Z1, Z2: any script or check you add ships intent examples with named sources and two oracles (see its "Second oracle").
Z3: on a contradiction, apply its Precedence list and report both sources; never choose silently.
Z4: no skip, xfail, disabled check, force flag or gate bypass without an override meeting its "Override rules"; report each one.
Z6: reversible and inside the spec, decide and record it in your report; irreversible, product-scope or spec-ambiguous, stop and raise it.
Z7: build the smallest change that meets the done-when; report net_lines and, above +150, what could have been deleted; reviewers answer "what could be deleted".
Z8: simplest thing that meets the done-when; the gate runs simplicity_check; reviewers answer the five checklist lines.
You may not edit docs/ZENKAI.md, contracts or CLAUDE.md. Reviewers: a Z1, Z2 or Z4 breach is at least Important.
```
