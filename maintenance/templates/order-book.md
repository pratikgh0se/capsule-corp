---
type: order-book
ai-first: true
---

## For future agent

This file is **hand-written by the owner and never machine-written**. It is the
order book (Capsule decision row 139, cluster C11 "Dragon Balls"): the five
sections below are where demand lives before it becomes work. It is
**never consumed by the bridge** - `intake.CONSUMABLE` is `("## New work",
"## Approvals")` in `Boards/Intake.md` only, and nothing in capsule-corp opens
this file. The Friday 20:00 weekly job READS it to prepare a betting table and
decides nothing.

Do not add cards here. Do not move lines from here into `Boards/Intake.md`
without a bet. See docs/RUNBOOK.md, "The order book", in the capsule-corp repo.

## Policy

- **One live bet.** One bet on deck, fully pitched, so a kill does not idle the
  pipeline.
- **~15 opportunities.** Over the cap the table drops one to add one. This is
  the anti-backlog rule: `Boards/Intake.md` already carries 122 migrated
  backlog lines that row 84 declared are not cards.
- **A kill verdict whose date has passed blocks the next bet.** Pre-commitment
  is the only version of a kill criterion that survives sunk cost.
- **One discovery hour a week.** The only unautomatable item in the structure.
- **The witness rule.** An opportunity needs
  **at least one dated, attributed witness**, stored raw and linked under
  `## Witnesses`; an opportunity line
  carries **no solution words** - it names a behaviour someone is failing at;
  and a pitch's `## Problem` cites a witness id that exists under
  `## Witnesses`. With no users, a witness is a conversation the owner had.
- **Enforced by reading until the first bet is stamped.** `pitch_lint.py`
  (M247), the eight-gate evidence bar (M248), the `bet:` approval verb and the
  `bets/<slug>.json` stamp (M250) are parked with that trigger.

## Live bet

<!-- exactly 0 or 1 line, this shape:
- <slug> · appetite <n> cards / <n> epic runs / <n>% window · one number: <metric> via <instrument> · kill by <YYYY-MM-DD>
-->

## On deck

<!-- 0 or 1 line, the same shape, and Pitches/<slug>.md must exist -->

## Opportunities

<!-- ~15 max, one customer problem per line, no solution words:
- <the behaviour someone is failing at> · witness: <id> · <YYYY-MM-DD>
-->

## Killed

<!-- one line per kill, never pruned - this is how an idea is let go without
being forgotten:
- <slug> · <YYYY-MM-DD> · <reason>
-->

## Witnesses

<!-- the raw text lives OUTSIDE this file; this is the index:
- <id> · <YYYY-MM-DD> · <who> · <link to the raw text>
-->
