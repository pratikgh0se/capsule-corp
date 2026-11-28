# Board store and card state machine

In short:

- **What it is.** The board store is one SQLite file per board that holds all cards, runs and events.
- **What it does.** One transition table decides every card status change, so no code path can move a card illegally.
- **Where the code is.** `capsule/db.py` and `capsule/board_schema.sql`.

## Purpose
The board is one SQLite file per board. It holds cards, their runs and events, and Capsule's own
tables. A single transition table governs every status change so no code path can move a card
illegally.

## Where it lives (files)
- `capsule/db.py`: connection helpers (`connect`, `connect_ro`, `txn`), `migrate`, `create_board`, `event`, `transition`, `TRANSITIONS`, `guard_get/guard_set` (compare-and-set flags).
- `capsule/board_schema.sql`: base tables (`tasks`, `task_links`, `task_comments`, `task_events`, `task_runs`, `task_attachments`, `kanban_notify_subs`), a schema inherited from the earlier prototype's kanban.
- `capsule/event_kinds.py`: per-kind payload allowlist for `task_events`; `capsule/row_schemas.py` and `contracts/schemas/rows/`: pinned shapes of operational rows.
- `capsule/lease.py`: claim, heartbeat, stale release, `end_run`. `capsule/checkpoint.py`: checkpoint chain. `capsule/journal.py`: per-run step journal (the worker appends; the tick, `landing_reverts` and `wall_clock` read it).
- `capsule/spool.py`: per-run request/report/decision files, validation and apply. `capsule/safe_write.py`: atomic symlink-safe writes. `capsule/jsonschema.py`: stdlib JSON-Schema subset.
- `capsule/card_fields.py`, `capsule/cardmeta.py`, `capsule/citations.py`: card fields (`check_cmd`, `check_expect`, `size_tag`, `risk_flags`, `workload`, dependencies in `task_links`), the `builds:` line, decision-row citations parsed from card bodies.
- `contracts/schemas/*.json`: boundary schemas (`report.v1`, `request.v1`, `review_verdict.v1`, `validator_result.v1`, `alignment_verdict.v1`, `decision.v1`, `postmortem.v1`, ...).

## Inputs and outputs
In: operations from the tick and the lander. Out: rows and `task_events`. Other processes open the DB read-only (`connect_ro`).

## State it reads and writes
Tables: `tasks`, `task_links`, `task_comments`, `task_events`, `task_runs`, `task_attachments`,
`kanban_notify_subs`, `outbox`, `outbox_key`, `slack_msg`, `guard_state`, `thread_owner`,
`requests`, `incidents`, `app_event_seen`.

## Key rules and invariants
- Card statuses and allowed moves: `TRANSITIONS` in `capsule/db.py` (`todo`, `ready`, `running`, `review`, `blocked`, `triage`, `scheduled`, `done`, `archived`). The table maps each target status to the statuses it may be entered from: a card enters `running` only from `ready`, and `review` only from `running`, `blocked` or `triage`. `transition` raises `ValueError` on an illegal `frm`.
- A claim is a compare-and-set on `claim_lock` with a TTL (`lease.claim`); a card with an open parent goes back to `todo` with a `claim_rejected` event.
- Documents from workers: only schema-valid files in `spool/<id>/requests/` apply; an invalid one is recorded and marks the run invalid (`spool.apply`, `_mark_invalid`), never read as evidence the work failed.
- `db.migrate` is additive and idempotent; it widens the `incidents.kind` CHECK in place when needed and sets the incident watermark.
- Event payloads have a per-kind allowlist (`event_kinds.py`, row 569, so a stray field cannot leak into the event stream): `db.event` prints a warning on stderr for an unknown kind or key and still writes the row.

## Failure modes and what happens
Unreadable board: seat counts report `unreadable` and dispatch holds (`capsule/seats.py`). A
spool write through a symlink is refused (`safe_write`). A crashed worker leaves no `run.json`, which the tick reads as a crash.

## Tests that pin it
`capsule/tests/test_db.py`, `test_lease.py`, `test_spool.py`, `test_spool_symlink.py`, `test_spool_row_schema.py`,
`test_event_kinds.py`, `test_row_schema_pins.py`, `test_jsonschema.py`, `test_schema_contracts.py`,
`test_safe_write.py`, `test_journal.py`, `test_cardmeta.py`, `test_citations.py`, `test_open_parent.py`.

## Related decisions and specs
`2026-09-24-capsule-corp-2-design.md` section 6.1 to 6.2 and 7.3; rows 376, 391, 416, 420, 569.

## Status
current.
