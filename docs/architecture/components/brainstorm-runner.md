# Slack brainstorm runner

## Purpose
Answers the owner's Slack brainstorm threads (M204 slice B2, D229/D232). A thinking partner only:
it cannot act, delete, approve or change rules, and says so, pointing to the terminal (D113: decisions and actions stay in the terminal session).

## Where it lives (files)
- `capsule/brainstorm_run.py`: `_Run.go` (one pass), `_Run.one` (one brainstorm), `_vault_notes`, `_build_prompt`, `_publish`, `locked`, `main`. Reply scrubbing is `brainstorm.clean` in `capsule/brainstorm.py`.
- `capsule/prompts/brainstorm.md`: the fixed system prompt. `capsule/brainstorm_check.py`: the Z2 second oracle (reads files and SQLite only, never imports the runner).
- `launchd/templates/__PREFIX__.capsule-corp-brainstorm.plist` (WatchPaths plus StartInterval 60; listed in launchd/unloaded (maintainer only, not in the public edition), not loaded).
- Seams elsewhere: `capsule/outbox.py` (`_with_edit` threads `brainstorm-reply:` rows, `_releasable` never holds them), `capsule/models/claude.py` (`session_id` limit gives `--session-id`), `capsule/slack_msgs.py` (`BRAINSTORM_REPLY`), `capsule/config.py` (optional route `routing.brainstorm`).

## Inputs and outputs
In: `<home>/boards/<board>/brainstorms/<id>/` with `meta.json` and `pending/<event_id>.json` (written by intake). Out: `turns.jsonl` lines (`role` owner or capsule) and one outbox `slack` row per reply, key `brainstorm-reply:<id>:<n>`, `thread_key` `<channel>|<thread_ts>` copied from meta only.

## State it reads and writes
`turns.jsonl` (append-only), `meta.json` (the runner changes only `session_id` and `session_live`, written with `brainstorm_store.write_meta` under `brainstorm_store.locked` on a fresh read), `_day.json` (replies today), `_fail.json` (backoff), `_run.lock` (one instance), `ws/` (empty cwd for the model).

## Key rules and invariants
- One model call per thread per pass over all pending text, route `routing.brainstorm` or Sonnet, turn budget 2, 180 s. `tools: ""`, no MCP, no `--add-dir`. First turn `--session-id`, later `--resume`.
- Owner text sits in `<owner_message>` tags and is data. Before each call the existing vault-context lookup (`vault_context.retrieve`, read-only) adds at most 3 notes inside `<vault_notes>` tags; any failure means no notes.
- Reply schema `{reply: str<=3000}`, scrubbed (`brainstorm.clean`: `scrub.scrub` from `maintenance/scrub.py`, then `slack.SECRET_RE`) before it is queued.
- Limits, each with one fixed line (not repeated while it was the last reply): 100 replies a day (`DAILY_CAP`) and weekly usage at 90% (`WEEK_PAUSE_PCT`) keep the pending text for later; more than 60 owner turns per brainstorm (`TURN_CAP`) consumes it.
- A closed brainstorm is never answered. Its vault note is written once by `brainstorm.publish` (`_Run._publish`); a failure is retried on the next pass.
- Crash safety: `turns.jsonl` is written before the outbox row; the next pass re-queues a turn that has no row, and redelivered event ids are dropped.

## Failure modes and what happens
Model error or empty reply: nothing is posted, pending stays, retries back off from 120 s to an hour. Flush failure: the row stays queued for the tick's flush. Weekly usage is read by `usage_guard.usage_window` (`maintenance/usage_guard.py`), which reads the weekly meter over local Claude Code transcripts (`capsule/usage_meter.py`) and returns unknown (no pause) when there is no reading.

## Tests that pin it
`capsule/tests/test_brainstorm_run.py` (StubAdapter, fake lookup, temp home, fake flush), `capsule/tests/test_brainstorm_intake.py`, `test_brainstorm_integration.py`.

## Related decisions and specs
D229, D232, D113, D88, D33, row 626; M204 spec; this page.

## Status
current, install-only until the Slack settings are done. PROTECTED additions are pending (architect).
