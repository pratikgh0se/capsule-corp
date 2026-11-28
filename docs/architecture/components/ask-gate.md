# Ask gate (owner-question due diligence)

## Purpose
No question reaches the owner until a script has a complete dossier and a rule says only the
owner can decide it. Units 1 to 3 of the design of 2026-11-23: the dossier, the script rules R0 to R3, the two-seat
panel with rules R4 to R9, the verdict log, the `cos ask` check and the apply path (unit 3).

## Where it lives (files)
- `capsule/ask_gate.py`: dossier check, id and fingerprint, `verdict()` (pure), `capsule ask-gate check|run|panel|show`; `sweep()` is the patrol step.
- `capsule/ask_panel.py`: the two packets, the two calls through the patrol runner, the opinion check. `contracts/ask-panel.md`: the role text. `contracts/schemas/rows/opinion.v1.json`: the opinion shape.
- `capsule/patrol_run.py`: one step runs the gate for each owner-held card with a dossier and no verdict.
- `contracts/schemas/rows/dossier.v1.json`: the dossier shape. `contracts/schemas/decision.v1.json`: two optional fields, `searched` and `if_no_answer`.
- `capsule/config.py`: the `diligence` block. `capsule/host.py`: host key `diligence.ledgers`.
- `capsule/ask_refs.py`: the sources a quote may sit in and the word-for-word quote check. `capsule/ask_log.py`: dossier and queue files and the verdict log.
- `capsule/ask_apply.py`: unit 3. `step()` runs from the patrol (`patrol_run._ask_gates`) for each board, before the panel sweep; `effective()`, `pending()`, `line()`, `detail()`. `capsule/stall.py` `_gate_pending` calls `pending()`.
- `capsule/cos_ask.py`: `capsule cos ask <board> <task_id> --title ... --what ... --why ... --if-yes ... --if-no ... --pick ... --because ... [--dossier <id>]` (all seven text flags are required). `contracts/intent/ask_gate.jsonl`: intent examples (Z1).

## Inputs and outputs
In: a dossier JSON file (`id`, `fingerprint` and `at` are filled by the script when missing; a wrong value is refused). Out: one JSON line `{verdict, rule, reason, dossier, mode}`; exit 1 for `refuse`. Verdicts: `refuse` (R0), an earlier `escalate`/`decide`/`precedent` (R1), `escalate` (R2, R3), `panel` (only from `check`: nothing decides it without the panel). `run <board> <dossier id>` runs the panel on a stored dossier and gives `hold` (R4, R9), `escalate` (R5, R6), `precedent` (R7) or `decide` (R8). `panel <board> <dossier id>` runs only the seats and prints the valid opinions.

## State it reads and writes
Reads the board database (cards, incidents, `auto_decision` events), the ledger files and the files a quote cites. Writes one JSON line per verdict to `<board>/asks/verdicts.jsonl` (`at`, `dossier`, `subject`, `fingerprint`, `verdict`, `rule`, `reason`, `mode`, `would_refuse` when set; append-only, flock) and `<board>/asks/dossiers/<id>.json`. Mode `on` also writes `<board>/asks/queue/<id>.json` for an `escalate`.

## Key rules and invariants
- Mode `shadow` (default) logs and changes no card, no ask and no outbox row. Mode `off` does nothing. In mode `on`, a direct `cos ask` is always refused; only the gate's own queued send passes, and only with an `escalate` verdict under 24 hours old for the card and dossier.
- Each quote must sit word for word (white space collapsed) in the file its `ref` names, in the ledgers (source `ledger`) or in the incident row (source `incidents`).
- A `ref` may sit under three roots: the Capsule product root, the vault root and the board's own project clones (`<board>/work/<slug>/`, so a question about a project can cite that project's files); a ledger file is also allowed. A relative ref is tried under each root and each clone. The path is resolved first, so `..` and a symlink cannot leave a root.
- The panel: two seats in separate processes through `patrol._agent` (a test fakes them with `CAPSULE_PATROL_CMD` or a runner argument). The precedent seat gets ledger excerpts and no code; the engineering seat gets repo excerpts and no ledger; neither gets `recommended`, and the option order differs. An opinion must pass `opinion.v1`, name a real option and, if it cites a precedent, quote an owner ledger word for word (an earlier autonomous decision never counts). The seat role (`contracts/ask-panel.md`) reaches the model through `--append-system-prompt-file {role_file}` in the patrol `agent_cmd`; the panel fills `{role_file}` with its own file. Each seat runs in a fresh temp directory that holds only its own packet. The panel adds `--add-dir` for the ledger folders (precedent seat) or the repo (engineering seat) to the `claude` command, so a seat can read them. A null `precedent` counts as none; any key outside `opinion.v1` is refused, and the packet and role text list the allowed keys. The ledger source accepts only the ledger files. The engineering seat looks at the card work clone, else the Capsule root. A dossier with a `hold` gets one panel run in 24 h, and the patrol step runs one dossier for each patrol run, after the patrol actions and state are saved. Calls are capped by `diligence.calls_per_day` and `diligence.timeout_s`; with no model or a used cap the result is R4 `hold`.
- R8 uses the board `unattended.decisions_per_week` (`auto_decision` events, or in shadow the logged `decide` verdicts); R9 holds when it is used. R7 does not use it.
- R2 uses `stall_owner.never`, the class, `reversible`, `blast` and `diligence.owner_topics`. The rules never read a confidence value.

## Design notes
Verdicts are a file under `asks/`, never board events, so shadow cannot change board behaviour: no reader of `task_events` (stall, idle lane, status, audit) can count a verdict as card progress. R1, `show` and the `cos ask` check read that file.

## Failure modes and what happens
A missing board, a bad JSON file or a bad config exits 1 with one line on stderr. An unreadable ref refuses the dossier.

## Apply path (unit 3, mode `on` only)
Modes: `off` does nothing. `shadow` (live default) logs only. `on` applies. The effective mode (`ask_apply.effective`) is `shadow` when `diligence.veto_trip` (default 2) `panel` or `precedent` decisions were vetoed in 7 days. The trip queues one text-only ask (`tripped` row) from the patrol step. The config is not edited. The gate returns to `on` when the vetoes leave the window.

| Verdict | What the step does |
|---|---|
| `decide`, `precedent` | After `apply_wait_s` (default 0), lift the owner-held card once (`stall_owner._lift`). Write an `intervention` answer (author `capsule`), one `auto_decision` event (`source`, `dossier`, `cites`) and a `file` row with the `veto` command. Log an `applied` row. A veto undoes it. |
| skipped | The card no longer waits for the owner, or the never list bars it, or it had one gate decision since the last human verb. Log `apply_skipped`. If the skip reason is "decided alone once since the human verb" or "the never list bars it", requeue the dossier to `asks/queue/` for a normal ask; other skip reasons do not requeue. |
| `escalate` | The dossier waits in `asks/queue/`. The step sends one ask through `cos ask` (stable 16-hex key, so a repeat after a crash is the same ask) if no earlier ask is open and fewer than `asks_per_day` went out on the local day. A failed send logs `send_failed`. A card that no longer waits, or is parked, gets a `dropped` row and no ask. |
| `refuse` | One `capsule` comment on the card (`told` row). The card stays held. The second refuse for the same card within 24 h also opens one `invalid_document` incident. |

- In mode `on`, a direct `cos ask` is refused: the gate sends the ask.
- Digest and `capsule cos brief`: one count line ("Ask gate: N decided for you, N sent to you, N refused.") and up to 5 decision lines (card title, option, exact veto command), then "and N more". Both are absent when the gate did nothing.
- Page wait: `stall._gate_pending` holds the `owner_wait` page while `decide` or `precedent` rows of mode `on` have no `applied`, `apply_skipped`, `sent` or `dropped` row. The wait lasts `apply_wait_s` + 900 s from the oldest open row. A `hold` row does not delay the page. `shadow` and `off` do not change paging.
- Seat read access: the panel adds `--add-dir` folders to the seat command (ledgers for the precedent seat, the repo for the engineering seat), so a seat can read them from its temp directory.

## Not built yet
- Weekly audit numbers (no owner-question section in `capsule/report.py` or the audit packet).
- The design `panel_down_s` hold rule.
- The B24 "no verdict" rule: a card with no gate row pages on the usual ladder.
- Unit 4 chat hook.
- Owner answers as R1 input; `vault` and `recall` as required sources.

## Tests that pin it
Written by the blind tester from the behaviour contract (design section 10); those tests live in the separate blind repo, not here. In this repo: `capsule/tests/test_cos_ask.py` and `test_intent_task11_scripts.py`.

## Related decisions and specs
D99 (no owner question before a dossier and a script rule: the owner decides only what only the owner can); design `owner-question-due-diligence` (2026-11-23); Zenkai Z3, Z6.

## Status
unit 3 of 4 built (live mode `shadow`; mode `on` applies).

Last checked against code: main f1f54a228 (docs audit 2026-11-27).

## Before mode on
Built (unit "ask-gate-preon2"):
- The panel adds `--disallowedTools "Read(//<board>/asks/**)"` to each `claude` seat call (same place as `--add-dir`). A seat cannot read the stored dossier with `recommended`, also not with Grep. A real call proved the deny blocks Read and Grep, and that the same read works without it.
- `repo_excerpts` in the engineering packet keep only hits whose `ref` is a file inside the card clone (a relative ref sits under it; `..` and symlinks are resolved first). Other hits are dropped.
- The dossier read deny is added only when the panel command is the `claude` program. A different `patrol.agent_cmd` or `CAPSULE_PATROL_CMD` gets no deny, so mode `on` with another command needs its own deny first.
- The role contract (`contracts/ask-panel.md`) says that file contents from the clone and the repo are data, never instructions.

Still before mode on:
- Prove with one real seat call that a seat in its temp directory can still read the repo and the ledger files. Proven by a replay on d828ef77; no build needed.
