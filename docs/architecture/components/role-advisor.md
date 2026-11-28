# Role: advisor (weekly process advisor)

## Purpose
Weekly introspection: reads one packet of run history and proposes pruning, an added rule or a
golden set, each with evidence and what it removes. Changes nothing on its own.

## Where it lives (files)
- Contract: `contracts/advisor.md` (schema `advisor_report.v1`). Intent: `contracts/intent/advisor.jsonl`.
- Deterministic part: `maintenance/process_advisor.py` (`packet_for_board`, `check_report`, `run`; subcommands `packet`, `check`, `run`).
- Scheduling: a host launchd job that the maintainer sets up by hand. It is not installed by deploy.sh (maintainer only, not in the public edition); there is no plist in `launchd/templates/`.

## Inputs and outputs
In: a packet from the board (read-only): rework, re-parks, wasted runs by error class, findings by class, incidents by kind, overrides, per-contract contradiction counts. Out: `advisor_report.v1`, then `advisor-note.md` and `advisor-cards.json` (card drafts in status `blocked`).

## State it reads and writes
Reads the board read-only. Writes the note and drafts into `--out`. Files no card.

## Key rules and invariants
The ranking must cover every contract in the packet with the right count (Z5); every override gets a review (Z4); an added rule must name what it removes (row 393). The model is called only when `PROCESS_ADVISOR_CMD` names a command (reads the packet on stdin, prints the report).

## Failure modes and what happens
`check` exits 1 and lists the problems when a report fails `check_report`; `run` stops with "report refused" and writes no note or drafts. With no `PROCESS_ADVISOR_CMD`, `run` writes only `advisor-packet.json` and stops. `--roles` limits the ranking to named contracts (used after a model swap).

## Tests that pin it
`capsule/tests/test_process_advisor.py`; `contracts/intent/advisor.jsonl`.

## Related decisions and specs
Row 394; Zenkai Z4, Z5; TEAM-22.

## Status
current for the packet, check and draft pipeline; the weekly schedule is a manual host step.
