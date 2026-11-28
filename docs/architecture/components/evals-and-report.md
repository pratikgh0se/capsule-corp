# Evals, report and cost accounting

## Purpose
Measure the factory without trusting a single number: an interventions-per-card report, weekly eval
metrics each with a second oracle, outcome events after landing, golden-set replay for model swaps,
and host-measured cost and tokens.

## Where it lives (files)
- Report: `capsule/report.py` (`python3 -m capsule report <board> [--md|--json]`), `capsule/report_notes.py`, `capsule/human_minutes.py` (estimated minutes per human action), `capsule/touch.py` and `capsule/touches.py` (human touches per landed card).
- Eval checks: `capsule/eval_check.py` (+ `eval_check.json` tolerances), `capsule/eval_tokens.py` (tokens oracle), `capsule/outcomes.py` (+14 d headline and +30 d tracked outcome events, `tick_pass`).
- Golden sets: `capsule/golden.py`, `capsule/golden_cli.py` (`capsule golden run <role> [--route ...] [--min-pass]`), `capsule/harness_gate.py` (advisory before/after comparator), `capsule/run_record.py` (start-state record for replay).
- Cost: `capsule/cost.py` (cost per card, advisory landing line), `capsule/token_accountant.py` (`capsule tokens`), `maintenance/pricing.py`, `maintenance/prices.json`, `maintenance/meters.py` (factory meters, read-only), `maintenance/usage_guard.py`.
- Hand ledger: vault note `Knowledge/Capsule Corp - <owner.name> acts ledger.md` (`report.ledger_name()`; oracle B for `owner_acts`).

## Inputs and outputs
In: board DBs, spool files, `intervention` events, Claude transcripts (usage and model fields only, never message text), the hand ledger. Out: report text or JSON, `eval_check` events (task id `eval`), outcome events, `contradiction` incidents with role `eval`.

## State it reads and writes
Reads boards, spools, transcripts under `~/.claude/projects/`. Writes events through the tick; the report is read-only.

## Key rules and invariants
- Once a day the tick checks each closed local ISO week since `outcomes.SINCE` (2026-10-18): `owner_acts` (A: `touches.evaluate`, B: hand ledger, checked 7 days after week close), `interventions` (A: spool files, B: `intervention` events, 1 day after), `fresh_tokens` (A: `task_runs` columns, B: transcripts joined by `run_id` to `run.json` to `session_id`; relative tolerance 0.15; more than 15% of A unjoined counts as disagreement). Agree when `|A - B| <= max(abs, rel * B)`.
- While `owner.name` is the default and the vault holds another name's acts ledger (`preflight.foreign_ledger`), `owner_acts` is not checked: no event, no incident, one `eval_owner_unset` event and one `needs_input` line a day.
- A disagreement opens one `contradiction` incident (role `eval`, key `eval:<metric>`), shown as an `EVAL INCIDENT` line at the head of `capsule report --md`.
- Fresh tokens = in + out + cache write; cache reads are separate. A run killed before `run.json` adds no tokens.
- A golden run uses a route with no `fallback`: a judgement role never falls back to a cheaper model. Exit 0 at or above `--min-pass`, 1 below (or a model call failed), 2 empty set, 3 unreadable config or unrouted role.
- The owner's name is the host setting `owner.name` (default `owner`, see public-edition): `touches.py` builds its decision-row actor patterns from it (escaped, same case rules). Report keys are `owner_*` (`owner_acts`, `owner_touches_per_card`, `owner_act_rows`, `owner_actions`); readers accept the old keys from before the rename (`report.owner_get`, `touches.LEGACY_METRICS` for stored `eval_check` events and tolerance files). The old author name and the configured name are aliases of `owner`.
- Minutes of the maintainer's time are estimates by action type (`human_minutes` config), not measurements.

## Failure modes and what happens
A missing ledger row after the grace period is a disagreement. A missing input is reported as unmeasured, not zero.

## Tests that pin it
`capsule/tests/test_report.py`, `test_report_notes.py`, `test_evals_report.py`, `test_evals_fixtures.py`, `test_eval_check.py`, `test_eval_tokens.py`, `test_eval10_headline.py`, `test_outcomes.py`, `test_touch.py`, `test_human_minutes.py`, `test_golden.py`, `test_golden_cli.py`, `test_harness_gate.py`, `test_cost.py`, `test_token_accountant.py`, `test_run_record.py`; `contracts/intent/report.jsonl`, `eval_check.jsonl`, `outcomes.jsonl`, `golden_cli.jsonl`, `cost_line.jsonl`, `token_accountant.jsonl`, `touch.jsonl`.

## Related decisions and specs
Evals spec (E1 to E4, EVAL-04, EVAL-05, EVAL-10); rows 391, 420, 584, 616, 617; this page, docs/superpowers/specs/2026-09-09-power-level-design.md (maintainer only, not in the public edition).

## Status
current. The auditor role that would consume these as a weekly packet is planned ([role-auditor](role-auditor.md)).
