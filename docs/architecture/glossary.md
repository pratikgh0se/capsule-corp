# Glossary

Plain words for the terms the Capsule code and docs use. Each row gives a short definition and
where to look in the code. Status: checked against the code at commit `3acc797a7`, 2026-11-27.
Read [overview.md](overview.md) first for the big picture.

## TL;DR

A person or the chief of staff files a **card**. A **tick** (one per **board**) hands the card to
a **role** (implementer, reviewers, and so on) as a **run**. Runs leave documents in a **spool**.
The **lander** pushes the checked commit. Anything that goes wrong becomes an **incident**.

## Work units

| Term | Plain definition | Where in the code |
|---|---|---|
| card (task) | One unit of work, written in words. It is one row in the `tasks` table. The code says "task" (`t_<8 hex>` ids); the docs and the CLI say "card". | `capsule/db.py` (`new_task`, `TRANSITIONS`), [data-model.md](data-model.md) |
| epic | A git branch (for example `epic/my-app`) that holds a group of cards. The board config lists the epics. Cards on an epic are built in one clone and later landed together. | `capsule/config.py` (`epic_branches`, `epic_for_branch`), `capsule/epic_sync.py`, `capsule/workspace.py` |
| board | One project's queue: one SQLite file, one config file, one tick. | `capsule/paths.py` (`board_db`, `board_dir`), `capsule/board_schema.sql` |
| lane | An epic branch seen as a place where work flows: its clone, its cards, its held cards. "Lane behind" means the lane lags `origin/main`. | `capsule/config.py` (`lane_meta`), `capsule/lane_behind.py`, `capsule/epic_clean.py` |
| run | One attempt by one role on one card. It has a row in `task_runs`, a run id (uuid) and a spool folder. | `capsule/worker.py`, `capsule/lease.py`, `capsule/run_record.py` |
| spool | The folder `boards/<board>/spool/<run_id>/` where a run leaves its schema-checked documents (`requests/`, `report/`, `done/`). Only the tick applies them. | `capsule/spool.py`, `capsule/safe_write.py` |
| intake | The way a new card enters a board: a JSON file in `boards/<board>/intake/`, ingested by that board's tick on its next pass. | `capsule/intake.py`, `capsule/cli_intake.py` |
| outbox | The durable queue of messages the tick sends (a log file, and Slack when it is on). | `capsule/outbox.py`, the `outbox` table |
| contract | Two meanings. (1) A role contract: a short file `contracts/<role>.md` that says what a role gets, sees, must do, reports and may not do. (2) The board's `contract_commands`: the commands a change must pass. | `capsule/contracts.py`, `contracts/*.md`, `capsule/contract_jobs.py` |
| routing | The `routing` block of a board's config. It maps each role to a model route (provider, adapter, model, turn budget). | `capsule/config.py` (`route`, `_validate_routing`) |

## Processes and places

| Term | Plain definition | Where in the code |
|---|---|---|
| tick | The one process per board that writes the board. Each pass runs a fixed list of steps. It never calls a model. | `capsule/tick.py` (`Tick.once`), `capsule/tick_guard.py` (the lock) |
| seat | A slot in a host-wide count shared by every board: running implementers, running reviews, "heavy" slots (reviews plus the lander), and blind testers. The caps are `max_review` and `max_heavy`. | `capsule/seats.py`, `capsule/config.py` (`CAP_DEFAULTS`) |
| tier | The runner tier of a card: `strong` (Docker container), `light` (macOS seatbelt), `mac` (seatbelt with Xcode). Read-only roles always use the host runner. | `capsule/runners/__init__.py` (`runner_for`), `capsule/dispatch.py` (`tier_down`) |
| lander | The code that re-checks a finished card's exact commit in a throwaway clone and, in `land` mode, pushes it. It runs the gates and the suites. | `capsule/lander.py`, `capsule/landing.py` |
| keeper | The chief-of-staff-side loop (a person or an agent) that lands work by hand when the lander is in `dry-run` or busy. A watchdog pages when it goes silent. | `capsule/keeper_watch.py`, `bin/capsule-own`, [lander-and-keeper](components/lander-and-keeper.md) |
| patrol | A scheduled check, every 30 minutes, of "why is nothing running". A script collects facts and acts on fixed rules. It starts at most one agent call per board for the rest. | `capsule/patrol.py`, `capsule/patrol_run.py`, `contracts/patrol.md` |
| Lookout | Two things with one name. (1) A tick step that writes board facts to a spool file for the dashboards. (2) A role that proposes observability coverage for an application. | `capsule/lookout.py`, `capsule/lookout_ship.py`; `contracts/lookout.md` |

## People and roles

| Term | Plain definition | Where in the code |
|---|---|---|
| CoS (chief of staff) | The maintainer's agent. It files cards, triages, stamps and talks to the owner. It passes `--author cos` on `capsule human` calls. | `capsule/human.py`, `capsule/identity.py` |
| implementer | The role that writes the change. Its model session runs on the host and edits only its own clone; every shell command it runs goes to a no-network container on the strong tier. | `contracts/implementer.md`, `capsule/runners/docker.py`, `capsule/worker.py` |
| reviewer | A read-only role that judges the diff. | `contracts/reviewer.md`, `capsule/tick_review.py` |
| validator | A read-only role that judges the contract outputs, not the diff. | `contracts/validator.md`, `capsule/validator_report.py` |
| guide | A read-only role that checks the change against the spec and rules on contradictions. | `contracts/guide.md`, `capsule/bundles_guide.py`, `capsule/tick_rulings.py` |
| blind tester | A role that writes tests from the card's interface only, in a private repo the implementer never sees. | `contracts/blind-tester.md`, `capsule/blind/` |
| Korin | The role that writes the post-mortem for an incident and names the fix and the test that prevents a repeat. | `contracts/korin.md`, `capsule/incidents.py` |

## Reliability terms

| Term | Plain definition | Where in the code |
|---|---|---|
| Zenkai | The reliability rules Z1 to Z8 that bind every agent and script. They are cited by item and never copied. | `docs/ZENKAI.md`, `capsule/tests/test_zenkai_mirror.py` |
| incident | A record of a crash, hang, contradiction, override, human touch or similar. It closes only through the gate: a fix card, a decision row, a note and a passing recurrence test. | `capsule/incidents.py`, `INCIDENTS_KIND` and `INCIDENTS_STATUS` in `capsule/db.py` |
| override | A reasoned, time-limited bypass of one review-stage check for one card. | `capsule/override.py`, Zenkai Z4 |
| stamp | A human or CoS approval that lets a protected file change: a role contract, intent examples, or the image tool list. It follows `capsule loophole`. | `capsule/loophole.py`, `contracts/intent/SHA256SUMS`, [gate-intent-contracts](components/gate-intent-contracts.md) |
| waiver | A time-limited release of a hold. Example: in blind gate mode, an owner `accept` that names an open fix card waives a missing blind result for 72 hours. | `capsule/blind/gate.py`, `capsule/diff_ceiling.py` |

## Decision ids

| Term | Plain definition | Where to read |
|---|---|---|
| "D" id (for example D5) | An entry in the decision log. | [DECISIONS.md](../DECISIONS.md) |
| "row" id (for example row 456) | An entry in the design-decision table. Code comments cite both kinds. | [DECISIONS.md](../DECISIONS.md) |

Some ids in the docs are local labels, not log ids. DECISIONS.md lists them.
