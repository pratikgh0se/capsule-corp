# Blind path (hidden tests, tester, host wall)

In short:

- **What it is.** The blind path is a second judge the implementer cannot see.
- **What it does.** A tester role writes acceptance tests from the card text only. They live in a separate repo, and only pass or fail results come back.
- **Where the code is.** `capsule/blind/` and `capsule/runners/host_wall.py`.

## Purpose
A second judge the implementer cannot shape. A `blind-tester` role writes acceptance tests from
the card's acceptance text and an interface snapshot only. The tests live in a per-board sibling
repo no implementer, reviewer or guide may read. The tick runs them against the card's work and
only results flow back. This page is the whole path; [gate-hidden-tests](gate-hidden-tests.md) and
[role-blind-tester](role-blind-tester.md) cover the gate and the role.

## Where it lives (files)
- `capsule/blind/cli.py`, `preflight.py`: `python3 -m capsule blind init|preflight <board>` (create the blind repo and the board's `blind_tests` config block; say whether a run could start). Local only. Only the `unittest` adapter has a harness runner (`BUILT_ADAPTERS`).
- `capsule/blind/dispatch.py`, `launch.py`, `scan.py`, `brief.py`, `snapshot.py`: start one tester run per card hash after the implementer dispatch (review-shaped: no lease, one review seat; container mounts only a fresh clone of the blind repo on `card/<id>`, the interface snapshot and deps, read-only). The `interface_commands` of a mac card (runner tier `mac`) run on the host under the Mac lane profile in a scratch copy of the clone, inside the lane's one-job lock (`runners/mac.py` `session`), never in docker; the lock is taken only when the card has `interface_commands`, and while another mac job holds it no tester starts and nothing is written.
- `capsule/blind/lint.py`, `red.py`, `checkout.py`, `settle.py`, `exempt.py`, `close.py`: validate the tester's `blind_tests.v1` report, lint, red check (5 runs of the staged tests on the card's base, each in a fresh container), landing on the blind repo's `main` (host git, fast-forward, sets `tasks.blind_commit`, emits `blind_landed`), bounce or triage, and the guide's ruling on `red_exempt` tests.
- `capsule/blind/run.py`: the blind run in a second strong container after the contract commands, `/workspace` read-only, one writable `/out`. `capsule/blind/gate.py`: gate-mode consumer (failures, hold, release, the two-round cap). `capsule/blind/unavailable.py`, `text.py`.
- `capsule/blind/harness/` (`api.py`, `unittest_runner.py`): the in-container harness, a protected path.
- `capsule/lander_blind.py`: the lander re-runs the card's blind tests at the recorded `blind_commit` against the exact commit it will push.
- `capsule/runners/host_wall.py`, bin/blind-wall.py (maintainer only, not in the public edition): the host wall (see [worker-runner-and-images](worker-runner-and-images.md)).
- `capsule/incident_blind.py`, `capsule/incident_leak.py`: the blind-mount incident (a container that mounts blind material) and the leak-lint incident.
- `capsule/contract_jobs.py`: runs the validator's contract commands off the tick thread.
- Contract and schemas: `contracts/blind-tester.md`, `contracts/schemas/blind_tests.v1.json`, `blind_result.v1.json`; intent rows `contracts/intent/blind_tester.jsonl`, `blind_runner.jsonl`, `blind_wall.jsonl`.

## Inputs and outputs
In: the card, its acceptance statements, an interface snapshot, the harness API. Out: tests under `cards/<id>/**` in the blind repo, a `blind_tests.v1` report, `blind_result.json` per run, `blind:<id>` rows for the validator, events (`blind_tests_reported`, `blind_landed`, `blind_gated`).

## State it reads and writes
The blind repo (a sibling of the project, written only by the tester run and by host git in `red.py`), `tasks.blind_commit`, `blind-work` and `blind-out` folders under the board and spool, board events. The implementer container never mounts any of it.

## Key rules and invariants
- The blind run line is one exact form, written by `capsule blind init` and checked by `capsule blind preflight`: `cd /blind && exec python3 -m harness.unittest_runner --card {card} /blind/cards/{card}` (the `--list` variant is also accepted). A wrapper, `;`, `&&`, `|`, extra words or other spacing fail preflight, which prints the safe line.
- Mode is `redesign.blind_tests`: `off` (default, nothing changes), `report` (rows marked `mode: report only` never fail or block a card), `gate` (rows count; a card lands only on green blind tests).
- Two oracles per run: the harness's `results.jsonl` and the runner's own stdout summary plus exit code must agree; `git status --porcelain` of the worktree before and after must match. Any disagreement is `inconclusive` and opens a `review_infra` incident.
- A board with no usable blind repo gets a `blind:unavailable` row, never a silent pass. A reason that holds for the whole board opens one incident and one alert.
- In gate mode a missing, stale, inconclusive or skipped blind result holds the card with a plain reason; a person's `accept` (author `owner`) that names an open fix card waives it for 72 hours (Z4: a waiver must be reasoned and expire). The second verifier round with failing blind tests holds the card with a decision for the owner (`blind_cap`).
- Blind tests count only if they fail on the base: a test that passes there needs a guide ruling and a `red_exempt` entry. A tester round that does not land is bounced, the second bounce (`settle.MAX_ROUNDS`) triages the card.
- Reviewer and guide see counts and failing ids only; the implementer sees one finding per failing test (id, given, expect, observed).
- A mac-tier card (D305) gets a blind tester like a strong card (the light tier none). Its blind suite and red check run on the host via `runners/mac.py` `run_blind` (through `mac.session(copy=False)`: sandbox-exec, Mac profile, the work read-only, writes only to an empty scratch box), inside `mac-lane.lock`; never in docker. The suite never waits for the lock on a pool thread: the validator's blind run uses the lock its contract job took when it started (`Pending.lane`, `blind/run.py` `_on_host`), a red check tries once and retries on the next pass, and the lander's gate-mode blind check (`lander_blind._on_host`) waits with the lane's reason (the card is skipped and checked again, nothing recorded). The tester's own model session still runs in docker as for a strong card and sees only the interface snapshot; the product's code never runs there. The contract-command split (host-only on the Mac lane, the rest in the container; see [role-validator](role-validator.md)) does not change this blind path: `mac.run_blind` is unchanged.
- The files that judge a blind run (`red.py`, `lint.py`, `settle.py`, `run.*`, `gate.*`, `preflight.*`, `lander_blind.*`, `incident_blind.*`, the harness) are in `workspace.PROTECTED`.

## Failure modes and what happens
Docker or image missing: `blind:unavailable`. A red-check runner failure is tried again on a later pass and the third (`MAX_RUNNER_FAILURES`) triages the card. A tester run that ends failed or timed out is not started again after 2 such runs for one card hash (`dispatch.MAX_TRIES`). One card's red check is held to `RED_BUDGET` seconds of a pass, one per pass. A host wall that cannot prove its refusal starts no session.

## Tests that pin it
`capsule/tests/test_blind_run.py`, `test_blind_gate.py`, `test_blind_harness.py`, `test_blind_red.py`, `test_blind_dispatch.py`, `test_blind_text.py`, `test_blind_reconcile.py`, `test_lander_blind.py`, `test_lander_blind_closed.py`, `test_lander_blind_holds.py`, `test_lander_blind_keeper.py`, `test_lander_blind_release.py`, `test_host_wall.py`, `test_host_wall_apply.py`, `test_host_wall_cli.py`, `test_host_wall_close.py`, `test_host_wall_live.py`, `test_host_wall_outage.py`, `test_host_wall_proof.py`, `test_host_wall_scale.py`, `test_contracts.py`, `test_schema_contracts.py` (the incident modules are covered inside the blind tests). Intent: `contracts/intent/blind_*.jsonl`.

## Related decisions and specs
Rows 601, 602 (D15 to D18, D161, D162); this page.

## Status
current, opt-in per board (`redesign.blind_tests`). Python (`unittest`) boards only.
