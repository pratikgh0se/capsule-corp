# Role: Korin (incident post-mortem writer)

## Purpose
Writes the post-mortem for an incident: root cause, lesson, mitigation, enforcer, recurrence test,
fix card and decision-row text. It may never close an incident. The record and the closure gate are
in [incidents-korin-flow](incidents-korin-flow.md).

## Where it lives (files)
- Contract: `contracts/korin.md` (schema `postmortem.v1`). Intent: `contracts/intent/korin.jsonl`; `contracts/shown/korin.jsonl`.
- Bundle: `bundles.korin`. Tick: `Tick._incidents`, `_korin_dispatch`, `_korin_inputs`, `_close_korin`. Readers: `capsule/incident_inputs.py`.
- Config: `routing.korin` (example: Opus, 30 turns) and the `korin` block (`enabled`, `since`, `vault`, `main_checkout`, `decisions_note`, `max_running`, `test_timeout`, `vault_push`, `fix_epic`, `fix_assignee`, `watch_days`, `blind_tests`, the path of the private blind tests repo).

## Inputs and outputs
In: the incident record, evidence paths, log tails (3000 characters each, `LOG_TAIL`). Out: `postmortem.v1`.

## State it reads and writes
Host run (`HostRunner.limits` in `capsule/runners/host.py` gives it Read, Glob and Grep) over the main checkout, the evidence dirs and `Knowledge/Incidents/`. The tick writes the note and the record.

## Key rules and invariants
A document applies only if it passes the schema, names this incident, gives a `test_<module>.<Class>.test_<name>` id (or shell id), names the incident in the decision-row text and contradicts no intent example. Two failed runs per incident (`KORIN_TRIES`) leave it `open` with one notice. Off until `routing.korin` and `korin.enabled`; `korin.enabled` also needs `korin.since` (earlier incidents are backlog and are never dispatched). A Korin card is never dispatched as work.

## Failure modes and what happens
See the incidents page.

## Tests that pin it
`capsule/tests/test_korin.py`, `test_incidents.py`, `test_incident_recurrence.py`, `test_loophole_korin.py`; `contracts/intent/korin.jsonl`.

## Related decisions and specs
Row 456; Zenkai rows 454, 455; Korin enablement spec 2026-10-24 ([incidents-korin-flow](incidents-korin-flow.md)).

## Status
current (opt-in per board).
