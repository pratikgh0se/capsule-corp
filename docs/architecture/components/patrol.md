# Patrol (the outer layer of the stall supervisor)

## Purpose
The tick finds stalls and acts on them each 30 s. The patrol is a second, separate check on a
30 min clock. It covers three blind spots: a stall the taxonomy does not know, a supervisor that
broke or says "no stalls" by mistake, and a dead tick or heal job. It is a script. It starts a model
only when the script finds work it cannot decide. Most runs cost no tokens. Design:
the internal stall-supervisor design note and section 9.4 of the unattended-mode design (neither note is in this repository; [stall-supervisor](stall-supervisor.md) covers the shipped behaviour).

## Where it lives (files)
- `capsule/patrol.py`: facts (`collect`), the agent call, `apply`, the exports `why`, `second_look`, `work_list`, `check`.
- `capsule/patrol_facts.py`: `second_look`, `why`, `work_list`, `cards` (read only SQL).
- `capsule/patrol_check.py`: `check`, `norm`, `refusal` (the document check; pure).
- `capsule/patrol_run.py`: `run`, the commands `patrol` and `why`.
- `capsule/stall_patrol.py`: the stall class `patrol_agent_down` (read by `stall.find`).
- `capsule/config.py`: the `patrol` block. `maintenance/heal.sh`: the `patrol` check.
- `launchd/templates/__PREFIX__.capsule-corp-patrol.plist`: `StartInterval` 1800, `RunAtLoad`. Listed in `bin/lib_jobs.sh` (CUSTODIAL).
- Agent role text: `contracts/patrol.md`. The default `agent_cmd` passes this file to the model with `--append-system-prompt-file {role_file}`. No agent definition or install step is used.

## Inputs and outputs
Commands:
- `python3 -m capsule patrol [--board B] [--dry] [--json]`. Exit 0 in all normal cases. `--dry` writes nothing (no verb file, no beat, no log, no state) and prints the actions it would take. It still runs the agent, so it can name them.
- `python3 -m capsule why [--board B] [--json]`. Read only. One object per idle lane.

Steps of one run, per board:
1. Take the lock (`patrol.lock`). A run still alive after 20 min is killed.
2. Facts: `stalls --json` (a failure is the fact `supervisor: "broken"`), the tick-watch object, the board summary (cards per status), pauses, the login answer, the second look.
3. Fixed actions: a `why` reason with a fixed action (set-aside, continue) is applied at once. No agent starts for that lane.
4. Work list: at most 3 subjects. Then, if no skip reason holds, one agent call. The agent has read-only tools and prints one JSON document. The script checks it and acts.
5. Write `patrol.beat` and one line in `patrol.log`.
6. Last, `patrol_run._ask_gates`: the ask gate (`capsule/ask_gate.py`, `capsule ask-gate`) judges one dossier of an owner-held card that has no verdict. It logs to `<board>/asks/` only and never stops the patrol. It is skipped on `--dry`, under a pause, with a failed login probe, or with `agent_cmd` null.

Work list (`work_list(facts, state, cfg, now)`):
- A stall at level `L2` or `failed`, age at least `patrol.min_age_s` (30 min), class not owner-only (`owner_wait`, `held_parked`), card not an owner hold (`parked`, `spec_stale`, a decision request), no open guide ruling run.
- A subject only the second look sees, in 2 runs in a row (`state["missed"]` is `{subject: runs}`). With `supervisor: "broken"` it joins at once. Reason `missed_by_supervisor`.
- An idle lane whose `why` reason is `unknown` (key `why:<lane>`; it goes first). A tick that stayed not ok for the minimum age.
- A subject with an agent look in the last 6 h, or with 3 looks, is left out (`state["looks"]` is `{key: [times]}`).

Second look (`second_look(conn, now)`): its own SQL, shared with no stall code (Z2). `kind: held`: a triage or blocked card (not an owner hold) with no open run for 30 min. `kind: idle`: a lane with ready cards, no open run and no run start for 30 min.

`why(conn, facts, now)`: one object per idle lane (ready work, no open run, no run start for 30 min). A board with a run start under 30 min ago gives an empty list. First reason that fits:

| reason | when | action |
|---|---|---|
| `known_wait` | a pause is held, or the tick is stopped or in a crash loop | `none` |
| `tick` | the tick is wedged or dead | `tick-restart` |
| `quiet_stop` | the last run closed with a quiet kind and 0 continues | `continue <card>` |
| `holder` | a holder card has no `aside` row | `set-aside <card>` |
| `all_aside` | the holder is already aside | `none` |
| `clone-dirty`, `branch-busy` | `idle_lane.find` names it | `none` |
| `unknown` | nothing above | `ask-agent` |

Not built in this unit: `brief_held`, `tier_down`, `backup`, `no_work` (a lane falls to `unknown`).

Document (`patrol.v1`): `{"subjects": [{"key", "diagnosis", "fault": "card|capsule|environment|unknown", "action": {"verb", "args"}, "why"}], "escalate": bool}`. `key` is the work item key (or its subject). A flat `{"verb": ...}` is one subject for the first item. Verbs: `reset`, `unblock`, `epic`, `park`, `set-aside`, `rerun`, `continue`, `note`, `tick-restart`, `kickstart`, `none`. An unknown or forbidden verb (`accept`, `close`, `override`, `archive`, `amend`), a subject not in the packet or a bare `none` for a `why` question (needs `known_wait`, `pause` or `cap` in the subject) makes the whole document invalid: no action, one `document:` line in `refused`, one failed call. Any other refusal drops one action and keeps the rest.

`check(doc, facts, state, cfg, now) -> (actions, refusals)`. `facts` may carry `work` (else it is computed) and must carry `cards` (from `cards(conn, now)`) and `tick` (`restarts_1h`).

Eligibility: `reset` or `unblock` only for a non-owner, non-Korin card in the right status, with no release by `capsule` in 24 h and under 2 since the last human verb. `epic` only for a ready card with no runs. `park` only for a Korin fix card with text `duplicate of <card>: <why>`. `continue`: 2 per card per 24 h. `rerun`: one per card per 24 h. `tick-restart`: not with 2 restarts in the hour (tick-watch state), 1 per board per 24 h for the patrol. `kickstart`: `heal`, `disk-relief`, `backup` only.

Escalation (3 steps, then it ends): the cheap call gives `none` with fault `unknown` (not for `why`), or `escalate: true`. One strong call follows if the board used under `strong_per_day` strong calls in 24 h and the stall had no strong call. A second `escalate` is ignored. If the strong answer is `none` (or silent) for a card: one `set-aside` entry and one incident evidence line "patrol could not decide: ...". For a board-level stall: one tick restart (if the caps allow).

Skip reasons (the beat `agent` field): `skipped:pause`, `skipped:login`, `skipped:off`, `skipped:cap`; `none` when there is no work; `ran`; `failed`. The check order is pause, login, off, cap.

## State it reads and writes
- Home files: `patrol.beat` (`{"at", "boards": {board: {"stalls", "second_look", "tick"}}, "agent", "agent_down", "agent_down_since", "ms"}`), `patrol.log` (one JSON line per run: the beat fields, per board `subjects`, `cheap`, `strong`, plus `actions` and `refused`), `patrol-state.json`, `patrol.lock`, `patrol/packet-<board>[-strong].json`.
- Caps are read from `patrol.log`: cheap calls (`calls_per_day`, default 8) and strong calls (`strong_per_day`, default 2) per board in 24 h.
- Verb entries: a file in `<board>/human/` with `author: "capsule"`, `tag: "patrol"` and `kind` set to the verb (`reset`, `unblock`, `epic`, `park`, `set-aside`, `rerun`, `continue`). The tick applies it. A kind the tick does not know yet is recorded as an `intervention` event and nothing else.
- The patrol writes the board file only to attach incident evidence (`incidents.attach`).
- Config (`patrol` block, per board): `agent_cmd`, `model` (`sonnet`), `strong_model` (`opus`), `calls_per_day`, `strong_per_day`, `min_age_s`, `timeout_s` (600).
- The stall record gets `patrol: {at, verb, n}` for each card verb.

The packet is a text file. Line 1 is the question; for a `why` item it starts with "Why", and it names each key, its class and the tick verdict. Line 2 is JSON. It holds `subjects`, `tick_watch`, `summary`, `supervisor`, `why`, `permitted_verbs` and the reply shape.

## Key rules and invariants
- The agent decides. The script checks and acts. The agent never writes the board.
- The patrol never does: `accept`, `override`, `close`, `archive`; land, push, deploy, `tick --once`, `bootout`; lift a human park, `spec_stale`, a first-screen, safety or `owner_rounds` hold; release a Korin fix card; edit any file; act in a pause.
- The agent command is killed at `patrol.timeout_s` (10 min) or 1.5 times that (strong). `{max_turns}` (20, strong 30) is filled for providers that use it; the default Claude command has no turn flag (`--max-turns` is not in `claude --help` 2.1.326).
- Login probe: once per day, on the first run of the day that may start an agent.
- 3 failed agent calls in a row (exit code, kill, no document, invalid document): the beat has `agent_down: true` and `stalls --json` has the item `patrol_agent_down`. The script keeps running.

## Install
No new dependency. The command is the same on every system: `python3 -m capsule patrol`, each 30 min.
- macOS launchd: deploy.sh (maintainer only, not in the public edition) renders `launchd/templates/__PREFIX__.capsule-corp-patrol.plist`. Do not load it by hand on a host that is not ready; use deploy.sh.
- cron: `*/30 * * * * CAPSULE_HOME=$HOME/.local/share/capsule python3 -m capsule patrol >> $HOME/.local/share/capsule/logs/patrol-cron.log 2>&1` (run it from the repo).
- systemd user timer: a `capsule-patrol.service` (`ExecStart=/usr/bin/env python3 -m capsule patrol`, `WorkingDirectory` the repo) and a `capsule-patrol.timer` (`OnBootSec=1min`, `OnUnitActiveSec=30min`).
- No model mode: set `"patrol": {"agent_cmd": null}` in the board config. Steps 1 to 3 and 5 still run, and so do the fixed `why` actions. The beat says `skipped:off`.
- Another provider: `agent_cmd` is an argv list (or one string). These tokens are filled: `{model}`, `{packet_file}`, `{role_file}`, `{max_turns}`. A token the command does not use is added as a last argument, in the order packet file, model, role file. A config that still names `--agent X` keeps that flag, and the role file path is still added as a last argument because the command has no `{role_file}`. The command must read the packet and print the document on stdout. The default (Claude) is `claude -p --append-system-prompt-file {role_file} --model {model} --restricted --tools Read,Grep,Glob --max-budget-usd 1 "<prompt with the packet path>"`.
- Test seams: env `CAPSULE_PATROL_CMD` (replaces a non-null `agent_cmd`), `CAPSULE_PATROL_NOW` (epoch), `CAPSULE_PATROL_TIMEOUT_S` (replaces `patrol.timeout_s`; the strong call gets 1.5 times it), `CAPSULE_PATROL_LOGIN_CMD`, `CAPSULE_PATROL_STALLS_CMD` (argv prefix, board name added; exit 0 or 1 with a JSON list is ok, else `broken`), `CAPSULE_TICK_WATCH_CMD`, `CAPSULE_LAUNCHCTL`. In Python, `patrol_run.run(home, boards, now, dry, probes)` takes the probes `stalls`, `agent`, `login`, `restart`, `kick`.

## Failure modes and what happens
- The agent times out, exits non-zero or prints no document: one failed call, no action.
- The heal check `patrol`: a beat older than 75 min gives one `launchctl kickstart` and the record `patrol fail`. A fresh beat on the next run gives `patrol resumed`. Still old gives one page (6 h throttle). With no beat file the check is skipped.
- The stalls command fails: `supervisor: "broken"`. The second look still runs and its subjects join the work list at once.

## Wiring to the rest of Capsule
- `contracts/schemas/rows/intervention.v1.json` allows `author: "capsule"` and `tag: "patrol"`. `human.is_cos` treats author `capsule` as not owner work, and `routine_nudges.is_routine_intervention` opens no `human_intervention` incident for any entry with author `capsule`.
- The tick applies the kinds `set-aside`, `continue` and `rerun` (`capsule/interventions.py`).
- `patrol_silent` (a beat older than 90 min) is read by the tick in `stall_owner` (`SILENT`); see [stall-supervisor](stall-supervisor.md).

## Tests that pin it
Written by the blind tester from the behaviour contract (separate repo).

## Related decisions and specs
Section 9.4 of the unattended-mode design (not in this repository); [stall-supervisor](stall-supervisor.md); [maintenance-jobs](maintenance-jobs.md); Zenkai Z1, Z2, Z6, Z8.

## Status
built (unit 7). `contracts/patrol.md` exists and the patrol label is not in launchd/unloaded (maintainer only, not in the public edition), so deploy.sh loads it.
