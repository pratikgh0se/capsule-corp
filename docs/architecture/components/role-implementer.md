# Role: implementer

## Purpose
Builds the card: edits code in the epic's clone, runs the contract commands, and hands off a
`report.v1`. The only role that writes code. It runs in the strong tier (its commands run in a no-network Docker container), or in the light (seatbelt) tier for a light card when `redesign.light_runner` is on, or in the Mac lane (`capsule/runners/mac.py`) for a mac card when `redesign.mac_runner` is on (`runner_for` in `capsule/runners/__init__.py`).

## Where it lives (files)
- Contract: `contracts/implementer.md` (verbs: comment, block, request, artefact, untrack; schema `report.v1`). Shown-to-model fields: `contracts/shown/implementer.jsonl`. Intent examples: `contracts/intent/implementer.jsonl`.
- Bundle: `bundles.implementer` in `capsule/bundles.py`. Launch: `Tick._claim_and_launch`,
  `Tick._launch` in `capsule/tick.py` (`_claim_and_launch` is in the `TickRuns` mixin,
  `capsule/tick_runs.py`). Run: `capsule/worker.py` runs the model on the host. In the strong tier the tick starts the container
  (`capsule/runners/docker.py`) and the model sends every shell command to it through the
  `mcp__capsule__exec` tool; the model edits its clone with host file tools. The light runner
  (`capsule/runners/host.py`) runs the shell under a macOS seatbelt profile instead.
- Workspace and git rules: `capsule/workspace.py`. Worker image: `docker/base/` and `docker/stacks/` ([image-record](image-record.md)).
- Report notes and checks: `capsule/report_notes.py`, `capsule/check_gate.py` (card `check_cmd`).

## Inputs and outputs
In: the card, spec section, cited decision rows, human notes, review findings and guide rulings from earlier rounds, prior run paths on a rerun, checkpoint if resuming. Out: `report.v1` (implemented, left undone, commands and exit codes, issues, `net_lines`), or `status: checkpoint` when its turn budget runs out; side documents `request.v1` (block, request, artefact, untrack).

A continue prompt (quiet stops, `capsule/stall_continue.py`): when an implementer run ends with nothing
decided, Capsule starts the next run itself. On a resume the run is the same model session and the prompt is
"Continue the task. You stopped without finishing: <missing>". On a re-dispatch the missing text arrives as a
guide ruling note. The missing text lists the unchecked `- [ ]` lines of the card and your `left_undone` items.
After a question it adds your question and: "Nobody is here to answer. If one command can undo the choice,
choose by the card and the pushed docs, write the choice in your handoff, and continue. If not, end with a
block that gives the options and the one you recommend." If your handoff names an `untrack` step, the prompt
says the harness performs it on each clone: do not wait for it. The `untrack` verb help says the host applies it after the run ends, so `git ls-files` still lists the path during the run: do not block on it.

## State it reads and writes
Reads and writes its workspace (`work/<branch-slug>/`) only. Never writes the board.

## Key rules and invariants
Route default Sonnet with 80 turns (`capsule/config.example.json`); with `redesign.escalation` a failed Sonnet streak hands to `implementer_restart` (Opus). No network for the shell (strong tier `--network=none`; the light tier's shell also has none); no secrets. May not change a spec section, public interface or contract; may not delete, weaken or move a guard test to satisfy a gate (row 568); may not edit `docs/ZENKAI.md`. A change touching a protected path fails the round and the file is restored. Net lines above +150 need one sentence on what could be deleted (Z7).

## Failure modes and what happens
`max_turns`, or a wall-clock timeout when the run has a session id: checkpoint chain (cap `caps.checkpoint_chain`); every implementer call gets the cap minus up to 300 s so the checkpoint call has time. Timeout without a session id, or crash: lease released, failure counted (`caps.failure_limit`), `crash` or `hang` incident. Invalid side document twice: `invalid_document` block.

Reads: the file tools read only the clone and `add_dirs` (the request folder, and the prior-run folder when earlier runs exist). See [read-guard](read-guard.md).

## Tests that pin it
`capsule/tests/test_contracts.py`, `test_intent.py`, `test_implementer_guard_line.py`, `test_check_gate.py`, `test_report_notes.py`, `test_shown_sets.py`, `test_workspace.py`, `test_tick.py`; `contracts/intent/implementer.jsonl`.

## Related decisions and specs
Spec Appendix A.4 of the Capsule 2 design; rows 356, 361, 384, 400, 417, 509, 568, 601.

## Status
current.
