# Role: guide

## Purpose
Judges alignment: does the work agree with the cited decision rows and the spec section. Also
answers typed requests, rules on contradictions between a judge and an intent example, and rules
on runaway cards. Reads no code and no diff (row 468).

## Where it lives (files)
- Contract: `contracts/guide.md` (schema `alignment_verdict.v1`; `decision.v1` for rulings; verbs comment, unblock, reassign, raise-tier). Intent: `contracts/intent/guide.jsonl`.
- Bundles: `bundles.guide`, `bundles.guide_ruling`. Tick: `Tick._guide_inputs`, `_rulings`, `_ruling_one`, `_apply_ruling`; `capsule/guide_notes.py` (scope check on `amendment` and `notes`), `capsule/autoamend.py`.
- Model default Opus, 6 turns.

## Inputs and outputs
In: spec, rows, plan, contracts; at review time the card and the packet. Out: `alignment_verdict.v1` (aligned or drifted; the tick adds an `aligned_uncited` path), `spec_conflict_rows`, optional `amendment`; `decision.v1` for a ruling.

## State it reads and writes
Reads text pushed by dispatch (rows, spec section). The tick finds the section numbers (`cited_sections` in `capsule/citations.py`) from `§N.N` anywhere in the card, or from the list after `spec` on a `cites:` line (`spec 3, 5.3, 6a (note); kit ...`; a letter suffix such as `6a` is part of the number, the list stops at the first item that is not a section number). The spec file is the path on the card's own `spec:` line (it must end in `.md` and sit inside the board repo; an absolute path into the repo maps to its repo-relative path). `card_spec_section` (`capsule/citations.py`) reads it from git at the same sha as the board spec, never the working tree, and caps it at 200 KB (`SPEC_MAX_BYTES`). If the path is outside the repo, not `.md`, missing, unreadable or too big, the tick falls back to the board spec and names the path in the brief. A card with no `spec:` line uses the board spec. Writes only its document. The tick applies rulings and amendments.

## Key rules and invariants
Order of judgement: a contradiction with a cited row means drifted, naming it; with the spec on an uncited point, drifted; a cited row contradicting the spec section is `spec_conflict_rows`, which gives a `spec_stale` hold (never an implementer reset). A drift or spec_stale hold also lists "Also failed this round:" (the round's reviewer and validator findings). `validator_report.hold_reason` builds it inside the 2000-char reason cap: the guide text shrinks first, the failures section keeps at most half the cap, and each validator line shows its cmd as 80 chars plus "..." so the failing excerpt stays. A row-less `drifted` of a guide that did cite the spec still reaches the rerun as a `guide: drifted (rows ): ...` finding, so its fix list is not lost. With no spec citation at all, a `drifted` is treated as `aligned_uncited`: logged, one notice, review continues. A guide may not edit code or write a contract (it proposes a diff card).

Stall ruling (`capsule/stall_ruling.py`): the guide may also rule on a held card (`release`, `release-capsule-fault`, `ask-owner`). With `ask-owner` the document must carry `provisional` and `reversible`. The ruling never accepts, overrides or closes a card, and never lifts a park, `spec_stale` or a safety hold. See [stall-supervisor](stall-supervisor.md).

The patrol agent is a separate role (`contracts/patrol.md`, read only tools); it is not a guide. See [patrol](patrol.md).

## Failure modes and what happens
Document invalid twice: `review_infra`. A guide ruling loop is capped by `RULING_RESETS`.

## Tests that pin it
`capsule/tests/test_guide_notes.py`, `test_autoamend.py`, `test_ruling_sweep.py`, `test_contracts.py`, `test_intent.py`, `test_tick.py`; `contracts/intent/guide.jsonl`, `auto_amend.jsonl`.

## Related decisions and specs
Spec Appendix A.2; rows 361, 364, 368 to 370, 378, 386, 403, 414, 419, 468; factory-autonomy spec 2.

## Status
current.
