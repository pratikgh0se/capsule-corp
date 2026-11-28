# Product-board landing (day branch, daily PR, publish, rhythm)

## Purpose
A product board does not land straight on `main`: each card lands on the board's own day branch,
and one draft pull request a day carries the batch to `main`, where the owner merges. The harness
board (this repo) still lands on `main`. Epics can also be published as draft PRs.

## Where it lives (files)
- `capsule/day_branch.py`: the one place that fetches `origin/<branch>`, bases a landing on it and catches it up with `origin/main`. `lander.branch` config (default git branch name `capsule/daily` for a new board (a branch name, not a folder), set by `capsule/cli_ops.py`; `capsule/config.py` `validate_lander_branch`).
- `capsule/daily_pr.py`: `python3 -m capsule pr daily B [--dry-run]` opens or updates the board's one draft PR from the day branch into `main` through `gh`.
- `capsule/publish.py`: `python3 -m capsule publish` puts each epic on GitHub as one draft PR with one comment per card, from `lookout/publish/<slug>.want.json` files. Off unless `{"lookout": {"publish": true}}` in the capsule home config.
  Ready-for-merge is bound to the PR head (M043): the whole-branch review marker carries the head and a hash of the review text (`capsule-review:<n>:<head12>:<sha256[:12] of review.md>`), a publish that moves the head of a ready PR runs `gh pr ready --undo` and clears the stored `ready`/`reviewed`/`reviewed_head` so the new head is reviewed again, and every publish rewrites the PR body with the current card list. A marker counts only when its review was authored by the authenticated `gh` user; markers from any other login are ignored.
- `capsule/rhythm.py`: the weekly calendar (`python3 -m capsule rhythm B --at ...`).

## Inputs and outputs
In: the board's landed commits, `gh`, want files. Out: draft PRs, `daily_pr` guard rows, `<slug>.result.json`.

## State it reads and writes
`guard_state` row `daily_pr` (written only after `gh` succeeded). Git: `origin/<branch>`. Nothing here pushes to `main`, forces, or merges.

## Key rules and invariants
- A day branch that does not exist yet is not a wait: the card is based on `origin/main` and the push creates it. A branch behind `origin/main` gets a merge commit built in a throwaway clone; the landing's push fast-forwards over it; published history is never rebased. A conflicting merge waits.
- The PR lists task ids from commits on `origin/main..origin/<branch>`, minus ids `main` already names; the board's `landing` events are only a cross-check.
- `publish`: the epic branch must start with `epic/` and may not be `main`, `master` or the lander branch; a want value starting with `-` is refused; one handler per want, no retry loop.
- `rhythm` is off unless `rhythm.enabled` is true and nothing in the tick consults it yet: it is a read-only calendar (dispatch, product landing and harness landing windows; rules: Saturday zero, Sunday review, Friday freeze of product landing, Wednesday epic pause of dispatch).

## Failure modes and what happens
`gh` failure: no guard row, retried on the next daily run. Merge conflict on the day branch: the landing waits and names the files.

## Tests that pin it
`capsule/tests/test_daily_pr.py`, `test_publish.py`, `test_publish_guards.py`, `test_publish_s2.py`, `test_rhythm.py`, `test_lander.py`.

## Related decisions and specs
LAND-03 (row 600, Ruling 1), LAND-10 (Lookout spec D4 section 5.7, row 478), COM-11 (rows 136, 450, 600, 629). Landing itself: [lander-and-keeper](lander-and-keeper.md).

## Status
current for day branch, daily PR and publish; rhythm is partial (calendar only).
