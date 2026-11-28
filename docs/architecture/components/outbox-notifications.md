# Outbox, Slack and quiet hours

## Purpose
Durable notifications. The tick writes a row in the same transaction as the state change; a sender
delivers it later, so a crash cannot lose a message and a Slack outage cannot stall the tick.

## Where it lives (files)
- `capsule/outbox.py`: `add`, `notify`, `flush`, `Router`, backoff, quiet hours, `send_local` (the `local` channel: one macOS notification by `/usr/bin/osascript`, 10 s limit; a failure is one stderr line, never an exception; with `AGENT_OPS_NO_SEND=1` it runs no command and appends one JSON line to `<board dir>/local-no-send.jsonl`).
- `capsule/slack.py`: nine message classes (seven glyph classes plus `ask` and `page` for routing v2), template, channel routing, sender (`chat.postMessage`, `chat.update`).
- `capsule/slack_msgs.py`: one self-editing message per problem (`needs_key`, `broken_key`). `capsule/slack_digest.py`: timed morning summary and reminder.
- `capsule/slack_answer.py` (`python -m capsule.slack_answer event`): the child process that decides
  a Slack click or thread reply and posts the reply. `capsule/answer_base.py`: its stdlib-only base
  (paths, read-only DB connect, spool and Lookout constants, Slack block and `source_ok`), moved
  unchanged from `paths`, `db`, `spool`, `lookout`, `slack` and `brainstorm_intake`, which re-export
  it.
- `capsule/obs_envelope.py`: factory envelope line (observability convention section 10).
- Shell: `maintenance/lib_send.sh` (`capsule_cli outbox-send`), `maintenance/messages.py` (per-day message budget), `maintenance/brief.py`. `capsule/interrupts.json` (protected whitelist of events that break quiet hours).

## Inputs and outputs
In: `notify(conn, cls, headline, fields, reply, ...)` calls from blocks, landing, incidents, tick. Out: a `file` row (always, except log-only classes with no text) and, when allowed, a `slack` row. A `page=True` line also writes one `local` row (the plain headline), with Slack off and with routing unset; `sound=False` plays no sound. The stall supervisor repeats the `local` row each 30 min ([stall-supervisor](stall-supervisor.md)).

## State it reads and writes
Tables `outbox`, `outbox_key`, `slack_msg`, `thread_owner`; `guard_state` keys `outbox.slack` and `outbox.backoff:<channel>`; `boards/<board>/outbox.log`.

## Key rules and invariants
- Classes and glyphs (`slack.CLASSES`): blocked (red circle), done, scheduled, running, incident, paused, needs_input, and under routing v2 `ask` and `page`; a message is a headline, at most 3 `key: value` lines and an optional `reply:` command.
- Slack allowlist: `LOG_ONLY_CLASSES = running, done, paused, scheduled` never create a Slack row; only classes that need the maintainer or say something is broken post (visibility spec section 6, card 7). With `slack.routing: "v2"` (spec 2026-10-29) the rule flips to a positive list, `POST_CLASSES = ask, page`; the morning summary (`scheduled`) still posts, by its marker. Every other class keeps its `file` row and never posts. The routing flag is stored in `guard_state` key `outbox.routing`.
- Quiet hours (`config.quiet_hours`: `enabled`, `start`, `end`, `timezone`, off unless `enabled`; default window 22:00 to 08:30; `timezone: "system"` follows the OS zone): during the window Slack rows wait, except `incident` and (v2) `page` (`QUIET_INTERRUPTS`). A `blocked` line waits for the morning unless it carries a word of a `QUIET_EVENTS` entry (`factory_down_or_stuck`, read from `interrupts.json`); if that table is unreadable, `QUIET_FALLBACK` (incident, blocked, page) applies. With `redesign.interrupts` on, only whitelisted events leave at once and the rest wait for the batch. File rows always pass. An unresolvable zone fails open (sends).
- Delivery: at most 10 Slack sends per pass (`SEND_CAP_PER_PASS`), exponential backoff 30 s to 900 s per channel, a row goes dead after 12 attempts. The Slack token is fetched through `bin/capsule-secret`, never logged.
- A keyed message edits the already-posted one instead of posting again.
- Answer path closure: `slack_answer.py` imports only protected modules, all in `PROTECTED`:
  `capsule/__init__.py`, `answer_base.py`, `answer_scope.py`, `human.py`, `jsonschema.py`,
  `row_schemas.py` and itself. `slack.py` and `brainstorm_intake.py` are imported inside `_handle`
  only (after the decision, and on the never-an-ask-answer brainstorm branch), so a worker edit to
  an open module cannot change who or what an answer applies to. `answer_base`, `row_schemas` and
  `jsonschema` (and their package/extension shadows) joined `PROTECTED`.
- `AGENT_OPS_NO_SEND=1`: the answer process posts nothing and appends each reply it would have sent
  to `<board_dir>/slack-replies-no-send.jsonl`, one JSON line per reply (mode 0600): `channel`,
  `thread_ts`, `text`, `kind` (`answered` or `refused`), `ts` (epoch seconds). `start` exits 1 on
  `error` (unusable config).

## Stall pages after L2 (unit 6)
A stall at `aside` or `decided` sends no `local` or `slack` row. After 24 h a stall pages the Mac each
`unattended.slow_repage_s` (21600) and Slack once a day. One `local` row says "needs you" when every
open card is set aside and no run is open, then once each 24 h. A decision Capsule made alone adds one
`file` row with the `veto` command.

## Failure modes and what happens
Slack down or auth error: rows stay queued, backoff holds the channel, the file log still has every line. A row past its attempt limit goes dead and is not retried.

## Tests that pin it
`capsule/tests/test_slack_answer.py`, `test_slack_answer_process.py`,
`test_sandbox_slack_protected.py`, `test_slack.py`, `test_slack_edit.py`, `test_slack_prefix.py`,
`test_slack_digest.py`, `test_quiet_hours.py`, `test_quiet_hours_system.py`,
`test_outbox_row_schema.py`, `test_no_live_send.py`; `contracts/intent/outbox_row.jsonl`, `quiet_hours.jsonl`.

## Related decisions and specs
Rows 398, 424, 425; docs/superpowers/specs/2026-09-10-communication-and-rhythm-design.md (maintainer only, not in the public edition); visibility spec (2026-10-21); this page (see Status).

- Slack listener (D225): `capsule/slack_listen.py` is the host-side Socket Mode intake only (acks at once, hands clicks and replies to the `capsule.slack_answer` subprocess); `capsule/slack_secret.py` holds the token reader, redirect refusal and log masking so its import closure stays small.

## Status
current. Drift: the older operating notes said done and running messages go to Slack; the code does not post them (see `LOG_ONLY_CLASSES`). The scheduled-class emitter and the needs-you cap are not built; of the `interrupts.json` events only `factory_down_or_stuck` has real Slack emitters.
