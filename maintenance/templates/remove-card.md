# Quarterly: what do we remove?

## Why

Capsule only grows unless something asks what can go (M256, decision row 139; row 114's redesign
pass was the first instance). Filed by the quarterly `remove-card` job; this card is the whole
ritual, so it is decided by hand.

## What to look at

- Scripts, checks, jobs and flags nothing has used since the last pass (docs/RUNBOOK.md
  lists them; `maintenance/status.sh` shows which jobs still run).
- Flags that have sat at their default for a quarter.
- Docs and tests that only describe something already removed.

## Done when

- [ ] Each candidate is either removed (a card per removal) or kept with one line saying why.
- [ ] The answer is written on this card, even if it is "nothing this quarter".
