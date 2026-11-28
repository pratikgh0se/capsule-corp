# Incidents (the Korin flow)

In short:

- **What it is.** An incident is the record of a crash, hang, override or human intervention.
- **What it does.** Korin writes the post-mortem, a fix card is filed, and the incident closes only through the gate.
- **Where the code is.** `capsule/incidents.py` and the `capsule/incident_*.py` modules.

## Purpose
Every crash, hang, review-infra failure, contradiction, human intervention, override and tick error
becomes a record with a post-mortem, a fix and a gate. An incident closes only through the gate,
never by Korin. A repeat of a closed fingerprint within 30 days is the headline reliability number.
A per-event kind (human intervention, override, escaped bug) whose fingerprint matches a `fix_pending` incident (closure not `watch`) records a recurrence on it and opens no new incident; with no such match it opens one as before.

## Where it lives (files)
- `capsule/incidents.py`: open, fingerprint, attach evidence, apply a post-mortem, closure gate. `capsule/incidents_cli.py`: `capsule incidents list|show|open|row|fixcard|attach|retest|landed|duplicate|close|close-history|app-event`. `capsule/app_events.py` backs `app-event` (take one `app_event.v1` file, or drain the board's `app-events/inbox`).
- `capsule/incident_attach.py` (name the fix card, this or another board), `incident_inputs.py` (log tails, `korin.since`), `incident_recurrence.py` (run and prove-new a recurrence test), `incident_blind_run.py` (run a `blind:` id from the private tests repo, return only pass or fail, the ran count and the tests repo HEAD), `incident_landed.py` (`incidents landed`: a fix built outside the factory), `incident_watch.py` (watch closure), `incident_blind.py` and `incident_leak.py` (classify the blind-mount refusal and the blind leak-lint count as `review_infra` incidents).
- Newcomers:
  - `capsule/incident_match.py`: the intent-example pattern language (`<empty>`, `<non-empty>`, `<any>`, `<not>`, `<each>`, `<some>`) that `incidents.postmortem_problems` uses to check Korin's document against `contracts/intent/korin.jsonl`; a pure helper split out of `incidents.py`.
  - `capsule/incident_duplicate.py`: `capsule incidents duplicate <id> --of <canonical>`; marks a live twin `superseded` (`closure = duplicate`, `duplicate_of` evidence line, one `incident_duplicate` event). Not a close, so repeat counting is unchanged. Refuses a missing, self, or superseded canonical, a non-live (or postmortem, or `closure = watch`) incident, a canonical closed before the incident opened, and differing fingerprints unless `--reason` is given; parks the twin's own fix card.
  - `capsule/incident_history.py`: `capsule incidents close-history`, run by the CoS from a reviewed list; closes pre-Korin incidents whose issue is no longer open as `closure = history`, without the Korin gate. Dry run unless `--apply` (which needs the list's sha256); refuses anything opened after its cutoff, recurred, worked by Korin, or whose card is still active.
  - `capsule/fix_card_drift.py`: when a human releases a held Korin fix card (`interventions.py` unblock and reset), `note_release` adds a "Main moved since this card was filed" section listing commits on the files the card names since the incident's recorded `main_sha`. Advice only; never refuses the release.
  - `capsule/fix_card_quotes.py`: when the tick files the fix card after applying a post-mortem (`Tick._close_korin`), `quoted` appends the text of each human file the report names, capped to the schema's body limit, because the builder reads only its own card's human files.
- `capsule/stall.py`: the stall supervisor. `stall.step` runs once per pass (it calls `docker_down.step` and the dead-man check `unowned_idle.check`) and finds held cards (triage and blocked), idle and unroutable lanes and a board with no progress. One record (`guard_state` `stall:<class>:<subject>`) and one incident per stall; the owner gets a Mac notification each 30 min and a Slack page at the start and each 2 h. See [stall-supervisor](stall-supervisor.md). A card stall attaches a `stall:` evidence line to the card's open cause incident; with none it opens one `hang` incident (key `stall: <class>: <letters>`; `stall: unknown: <board>` for `no_progress`). A detector that raises opens one `crash` incident per day (key `stall supervisor: <ExceptionType>`). The old `lane-stall:` incident of `epic_clean` is gone.
- `capsule/tick.py` `_incidents`, `_korin_dispatch`, `_incident_gate`, `_close_korin`; `capsule/invariants.py` (`KINDS` names three invariant kinds; `sweep` raises two); `capsule/close_routine_nudges.py` (one-off CoS clean-up).
- Role: [role-korin](role-korin.md). Schema: `contracts/schemas/postmortem.v1.json`.
- `capsule/db.py`: `incidents` table and its `kind` CHECK widening.

## Inputs and outputs
In: typed events past the incident watermark; by hand, `incidents open` (`--root-cause-key` a slug of at most 80 characters, hashed into the fingerprint; `--description` free prose, stored in `incidents.description`, never hashed, given to Korin's input). Out: an incident row, a Korin card and run, a blocked fix card, a vault note `Knowledge/Incidents/<date> - <kind> - <id>.md`, outbox lines.

## State it reads and writes
`incidents` table, `guard_state` (`incidents:watermark`), cards, the decisions note (read), the vault note (write), the main checkout (read, recurrence tests run there with a throwaway `CAPSULE_HOME`).

## Key rules and invariants
- Always on, every tick pass: one incident per typed event past the watermark: `crashed` gives `crash`; `timed_out` gives `hang`; a `review_infra` block gives `silent_failure` or `review_infra`; an `invalid_document` block gives `invalid_document`; `contradiction`; each `intervention` gives `human_intervention`; each `override` gives `override`; `tick_error` gives `crash` (or a `review_infra` for the blind-mount refusal); `contradiction_loop` gives `contradiction`; `runaway` gives `runaway`; `worktree_mismatch` gives `crash`; a `blind_gated` event with leaks gives `review_infra`. Events on Korin's own cards open nothing. By hand: any kind but `repeat` (for example `escaped_bug`, which the lander also opens on a red push).
- Fingerprint: first 16 hex of sha256 of `<kind>\n<role>\n<root-cause key>`, with volatile parts masked. Same fingerprint on the same UTC day attaches evidence (human interventions and overrides never dedupe). A fingerprint closed in the last 30 days opens kind `repeat`.
- Korin runs only when routed and `korin.enabled`; a document applies only if it passes the schema, names this incident, gives a recurrence-test id and contradicts no intent example. Applying it fills the record, creates a blocked fix card (`korin.fix_epic` or the incident card's epic) and moves the incident to `fix_pending`.
- Closure gate (`close`, also run by the tick): record fields filled; fix card `done` with a `completed` event; `decision_row` set and a `| <row> |` line in the decisions note names the incident; the note exists and names it; the recurrence test was not on `main` at the commit recorded when the incident opened and is on `main` now; the main checkout is on `main` and the test exits 0 with at least one test run. `closure = watch` (low or medium confidence, not for `repeat` or `escaped_bug`) closes an unproven one-off after `korin.watch_days` (default 14) with no same-fingerprint incident. Recurrence ids may be dotted unittest ids `tests/test_<name>.sh[::label]`, or `blind:<group>/<file>::<Class>.<test>` (the group folder may hold hyphens, not first; a test in the repo named by `korin.blind_tests`; nobody reads it, D161/D17). A blind id is new when it is red (ran >= 1, non-zero exit) at the incident's `main_sha` and green on `main`; ran 0 is an error. The tests repo HEAD goes into the incident evidence. With `korin.blind_tests` unset a blind id is refused. `incidents landed <id> --sha <sha> --author cos|owner` moves a blocked, run-less, same-board fix card to `done` with a `completed` event `{commit, outside, author}`, so the gate can pass for a fix built outside the factory; it refuses a sha where the recurrence test is not new since the incident base, and an incident with no recurrence test (a `closure = watch` incident skips this check, as the close gate does; a `blind:` id skips it here, and the close gate proves it red then green instead); it sets an empty `main_sha` to the last main commit before the incident opened. `--renamed-from <old id>` (with `--test <new id>`) is for a test renamed after the fix: the novelty proof reads the old id at the fix sha, the new id must be new on main and green there, and the evidence line records the mapping. `incidents close` takes `--author` too (agent sessions pass `cos`); it is the `by` of the `incident_closed` event. The test finder reads the module with the Python parser, so a wrapped `def` is found.
- The tick runs at most one recurrence test per pass and re-runs a failing one at most hourly (`RETEST_SECONDS`).
- Invariant sweep (`capsule/invariants.py`, every pass, needs `invariants.enabled` and `invariants.dev_checkout`): `main_checkout_write` (the tick runs inside the dev checkout, or that checkout is dirty) and `lost_card_state` (a card's status disagrees with its events) each open an incident and send one `needs_input` notice. Only the first case (the tick's working directory is in the dev checkout) also pauses the board, until the maintainer clears `pause:board`. The `two_dispatchers` kind exists in the schema, but no code raises it today.

## Failure modes and what happens
Two failed Korin runs: incident stays `open`, one outbox notice. Failed note write or push: outbox line. A recurrence inside a watch window opens a `repeat` and marks the watched incident `superseded`.

## Tests that pin it
`capsule/tests/test_incidents.py`, `test_incidents_cli.py`, `test_incident_recurrence.py`, `test_incident_watch.py`, `test_korin.py`, `test_invariants.py`, `test_close_routine_nudges.py`, `test_incident_history.py`, `test_fix_card_drift.py`, `test_fix_card_quotes.py`; `tests/test_invariants.sh`; `contracts/intent/korin.jsonl`, `invariants.jsonl`.

## Related decisions and specs
Rows 454 to 456, 507; Zenkai Z1, Z2, Z4; Korin enablement spec 2026-10-24; this page. App incidents (widening to product apps) are planned: [../planned.md](../planned.md).

## Status
current.
