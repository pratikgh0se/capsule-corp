# Gate: protected paths

## Purpose
A worker must not be able to edit the files that judge it. `PROTECTED` lists those paths.

## Where it lives (files)
- `capsule/workspace.py`: `PROTECTED`, `touches_protected`, `restore`, `changed`.
- `capsule/tick.py` `_verdict`: review-time check on the worker's change (the failure text comes from `protected_scope.py`).
- `capsule/lander.py` `_protected`: landing-time check on the commit range. `capsule/protected_scope.py`: the reference tree for the review-time check.

## Inputs and outputs
In: a list of changed paths. Out: the subset that matches.

## State it reads and writes
None; `restore` rewrites the clone's protected files to the round's start sha.

## Key rules and invariants
- Current list: `capsule/workspace.py` `PROTECTED` is authoritative; read it there. Families:
  `contracts/**`, `docs/ZENKAI.md`, `SOUL*.md`/`CLAUDE.md`/`AGENTS.md`, the judging modules
  (`intent.py`, `workspace.py`, `lander.py`, `lander_blind.*`, `unblocked_stop.py`, sharding, the
  image renderer, the blind red/lint/settle/run/gate/preflight files and `blind/harness/**`,
  `incident_blind.*`), the Slack answer path (`slack_answer`, `answer_base`, `answer_scope`,
  `human`, `row_schemas`, `jsonschema`), `docker/base/**`, `docker/stacks/**`, `docker/IMAGES.lock`,
  `docker/build-base.sh`, `calibration/**`, `maintenance/meters.py`, `capsule/interrupts.json`, the
  lander's own gate scripts under `tests/` (`mutation_check.py`, `size_check.py`,
  `simplicity_check.py`, `ruff_gate.py`, `portability_lint.py`, the size baseline and the mutation
  helpers), deploy.sh (maintainer only, not in the public edition), `bin/capsule-unblocked-hook.sh`, bin/blind-wall.py (maintainer only, not in the public edition), and the sandbox
  runner. `capsule/protected_scope.py` decides what the protected files should be when the host (not
  a run) moved the lane onto main, so a host merge is not counted as a worker edit. Some entries use
  wildcards that also cover a package folder or extension module. Also protected: capsule.toml (the image tool list),
  the docker, worker and Claude-adapter runners, the Slack listener and brainstorm modules, `cos_ask.*`,
  `engine_heal.py`, `safe_write.py`, and the lander's gate helpers `discover_tmp.py` and `mutation_env.py`.
  A `tests/` or `maintenance/` file named like a standard-library module is flagged too, because the gates
  would import it first (`workspace._shadows_stdlib`).
- Matching is case-insensitive and also flags a bare directory path that is a glob's root or ancestor, so a case-colliding or symlinked spelling cannot hide.
- Review time: a protected change fails the round (`protected path changed (row 395)`) and the file is restored, so a planted file cannot reach the next run.
- Landing time: any protected change in the range, including a human's, holds the card for the maintainer's stamp (`protected` hold). A changed file under a `SHA256SUMS` must match its line.

## Failure modes and what happens
Review: round fails, file restored. Lander: hold named `protected`, card waits for a stamp.

## Tests that pin it
`capsule/tests/test_workspace.py`, `test_lander.py`, `test_worker_image_protected.py`; `contracts/intent/protected_paths.jsonl`.

## Related decisions and specs
Rows 395, 470; redesign spec section 6; worker-images spec section 1; blind spec 4.4 item 7.

## Status
current.
