# Chief-of-staff tools and routine maintenance scripts

## Purpose
The chief of staff (CoS) agent and the maintainer do the same repetitive operations every day.
Decision row 643 says common maintenance is scripted, never improvised. These are those scripts,
plus the CoS-facing commands.

## Where it lives (files)
- `capsule/cos_ask.py`: `capsule cos ask`, one plain-English decision for the owner (text linted: no ids, paths or commands), queued as class `ask`; an ask file under `boards/<board>/asks/` records what each answer does, so a later listener trusts the file and never the Slack text. `capsule/cos_brief.py`: `capsule cos brief`, the live briefing (status screen, open asks, answered while away).
- `capsule/status_cli.py`, `statusview.py`, `boardview.py`: `capsule status` and `capsule board`.
- `maintenance/landing_prep.py`: pack reviewed branches into one keeper batch (preview by default; `--apply` squashes into a new `keeper/batchN` worktree and reports unioned conflicts as unreviewed).
- `maintenance/after_landing.py`: the post-landing chain: fast-forward the live checkout, ./deploy.sh (maintainer only, not in the public edition) and `--check`, sync the live `images.lock` stack rows to origin/main's `docker/IMAGES.lock` when that image is local (under the shared record lock), warn with a `STALE record row` line when the landing changed a layer source (`docker/base/`, `docker/stacks/python/`, `docker/stacks/web/`), restart ticks whose code changed (otherwise fast-forward the release clone to origin/main), `--check` in the release clone, image sweep, summary. No flag is a preview.
- `maintenance/branch_sweep.py`: remove local `keeper/*` and `build/*` branches whose work is on `origin/main`, and the work folders that hold them; keeps `main`, `epic/*`, `wip/*`, owned and carded branches.
- `maintenance/storage_gc.py`, `maintenance/disk_relief.py` (D204: every 4 hours, only below `OPS_DISK_RELIEF_FREE_GB`, default 60), `maintenance/hog_check.py` (frozen helper processes; preview by default), `maintenance/ops_preview.py` (the every-other-day preview of branch sweep and storage gc), `maintenance/board_query.py` (read-only SELECT over one board for the agent-review job).
- `bin/capsule-own` (branch ownership registry), bin/keeper-pause (maintainer only, not in the public edition), `bin/capsule-park.sh`, `bin/hoi-poi.sh`.

## Inputs and outputs
In: flags (none means preview) and the repo, board and docker state. Out: a printed plan or result; one JSON line per applied item in `storage_gc.jsonl`.

## State it reads and writes
Local git branches and worktrees, docker containers and images labelled as Capsule's, caches, `$CAPSULE_HOME/queue/<repo>/owners.json`. Never forced: no `-f`, no prune; each removal is one id re-checked before and verified after.

## Key rules and invariants
- Preview by default; `--apply` is explicit. Touches only what Capsule made and can prove it made; the rest prints `kept: <reason>`.
- Disk relief is the one automatic delete and runs fixed rules, no AI (D204, an exception to the no-delete rule for that list only).

## Failure modes and what happens
A section that fails or an item that will not go exits 1 with a one-line reason on stderr. After a partial restart the chain refuses and names the boards still to restart with their exact command.

## Tests that pin it
`capsule/tests/test_cos_ask.py`, `test_cos_brief.py`; `maintenance/tests/`; `tests/` shell and python suites for each script; `contracts/intent/after_landing.jsonl`, `branch_sweep.jsonl`, `landing_prep.jsonl`, `storage_gc.jsonl`, `ops_preview.jsonl`.

## Related decisions and specs
Row 643; D204 (auto disk cleanup); notification rebuild spec 2026-10-29; [maintenance-jobs](maintenance-jobs.md). Jobs: [maintenance-jobs](maintenance-jobs.md).

## Status
current.
