# Decisions behind the docs

This page explains the decision ids (such as "D123" and "row 456") that the Capsule docs and the public code cite. It covers the ids cited in the docs; the ids cited only in public code comments are accounted for in the tables at the end.
The ids match the ones cited there. "D" ids come from the decision log; "row" ids come from the design-decision table.
The full internal log is not published. Only decisions that explain how Capsule works are written here, in plain words.

Each entry says what was decided (as the code behaves today), why, and where it lives. Each entry starts with a status: built, partly built, planned or replaced.
A few ids in the docs are local labels, not log ids: `D1`/`D2` in the stall-supervisor page name stall classes, `D4`, `D9`, `D11` and `D12` name sections of older design studies or test fixtures. They are not listed here.

## D decisions

### D5 - One slim base image plus add-on layers
Status: built.
Worker images are not one image for every role. A shared base image gets add-on layers per kind of project, declared in the project's own tool list. An automatic image builder builds, tests and pins an image when a card is blocked by a missing tool.
Why: projects need different tools, and one image would not stay small.
Where: `docker/`, `capsule/image_requests.py`, `capsule/image_toml.py`.

### D6 - Workers install nothing while they run
Status: built.
Every tool is built into the project's image. A worker has no network and cannot download or install anything during a run.
Why: runs repeat the same way, run fast, and code-writing agents fetch nothing arbitrary.
Where: `capsule/runners/docker.py`.

### D7 - The image builder may change only a project's own tool list
Status: built.
The shared base image and the language layers stay under maintainer control.
Why: it limits what the automatic builder can break.
Where: `capsule/image_requests.py`.

### D8 - Image rebuild limits
Status: built.
Each missing-tool card gets 2 build tries. Each project gets at most 3 such cards a day and 60 minutes of building a day. After that, the work waits for a human decision.
Why: rebuilds must stay bounded in cost and time.
Where: `capsule/image_ledger.py` (a human unblock that lifts a limit is logged as an override).

### D10 - Disk budget
Status: built.
Worker images may use up to 20 GB in total, and the disk keeps at least 25 GB free after a build (`capsule/image_requests.py`: `ALL_CAP`, `FREE_AFTER`). A separate floor, the value in `docker/BUILD_MIN_FREE_BYTES` (40 GB), is the free space needed before a rebuild starts; a rebuild waits below it.
Why: storage on one laptop is limited.
Where: `docker/BUILD_MIN_FREE_BYTES`, `capsule/image_requests.py`.

### D15 - A private test repo per project, written only by the blind tester
Status: built.
Why: tests stay independent of the developers.
Where: `capsule/blind/`, `contracts/blind-tester.md`.

### D16 - Blind tests are a card's real pass/fail check
Status: built.
A card never passes while its blind tests are pending or unclear.
Why: no passing on unverified work.
Where: `capsule/lander_blind.py`, `capsule/tick_review.py`.

### D17 - Only results and examples leave the blind tester
Status: built.
Nobody else reads the test code: not the implementer, not the maintainer, not the reviewer. Tests run against a read-only copy of the work. A blind run's error text is withheld from the next implementer brief.
Why: a test that the author of the code can read is not independent.
Where: `capsule/runners/host_wall.py`, `capsule/blind/`.

### D18 - Blind-test details
Status: built.
Any change to a landed blind test is a risky card. Existing tests stay where they are. The tester sees only a snapshot of the outside interface. Tests run again at merge.
Why: part of the blind-tester design.
Where: `capsule/blind/`, `capsule/lander_blind.py`.

### D33 - Dispatch-time notes for workers
Status: replaced by row 626, which holds the current mechanism.
At dispatch, the tick may attach a few relevant notes from the maintainer's notes vault to a card's briefs. Only work notes may be copied into a brief. Workers still never search the vault themselves. Row 626 holds the current mechanism.
Why: fewer repeated debates and less drift from earlier decisions.
Where: `capsule/vault_context.py`.

### D66 - Secret-file protection started warn-only
Status: replaced by D215.
Capsule first warned when an AI agent tool call would read a secret file, and did not block it.
Why: check for legitimate reads before blocking. D215 then switched it to block.
Where: bin/secret-read-guard.py (maintainer only, not in the public edition).

### D77 - A small core, extras as add-on packs
Status: planned.
Extra roles, jobs and dashboards arrive later as packs. The pack loader is built only when two real packs need it.
Why: keep the core small (rule Z8). No loader code is on `main`.
Where: `docs/architecture/planned.md`.

### D79 - Incident system widened to cover shipped apps
Status: built.
Failures of apps that Capsule builds become incidents too, with a defined path when one fails, instead of a separate support agent.
Why: one incident loop is simpler than two.
Where: `capsule/app_events.py`, `docs/architecture/components/incidents-korin-flow.md`.

### D84 - Card pre-flight claim check
Status: built.
Before a card's first dispatch, a plain script (no model call) checks that the repo paths the card names in backticks exist. A card that names something missing goes to a guide for a quick fix.
Why: cards that name files that do not exist waste whole runs.
Where: `capsule/claims.py`, `bin/precheck-gates.py`.

### D85 - Hollow-test check for every language
Status: partly built.
A warn-only detector reads changed tests (Python, Swift, TypeScript/JavaScript) and flags tests that cannot fail. Each language's mutation tool is added when that language's worker image exists.
Why: the mutation check covers Python only.
Where: `capsule/hollow_tests.py`.

### D88 - Terminal channel plugin and resumable brainstorm handoff
Status: built.
A Claude Code plugin pushes factory events into the terminal, and a brainstorm can be handed off and resumed by name.
Why: the maintainer sees events where he works.
Where: `claude/capsule-chat/`, `bin/brainstorm-handoff.py`.

### D89 - Watching shipped products merges into incidents
Status: partly built.
Crash tracking and uptime checks for shipped apps report into the incident system (D79), so later apps inherit them.
Why: one place for every failure.
Where: `capsule/app_events.py`. The rest is in `docs/architecture/planned.md`.

### D90, D92, D94, D96 - Dragon Radar
Status: planned.
One weekly scout job replaces the older scout, model-watcher and tech-radar jobs. It has ten research tracks, exists only to make Capsule itself better, and every item must say what it changes for Capsule or be cut. Cheap models gather, a strong model ranks, and suggested cards wait for human approval.
Why: one job instead of several overlapping ones.
Where: `docs/architecture/planned.md`.

### D93 - Security-testing pack
Status: planned.
A pack of free security tools runs against throwaway copies only, driven by one security agent that must give a working reproduction per finding. Stages: attack Capsule itself, then every public app before release, then cluster configuration review.
Why: findings must be real and the real system must stay safe.
Where: `docs/architecture/planned.md`, `contracts/security-engineer.md`.

### D97 - Shell scripts move to Python
Status: partly built.
New scripts are Python. Existing non-test shell scripts are ported, riskiest first, and each port is proved by running the old shell tests against the new Python (two oracles) before the switch. Tiny starters stay where launchd needs them.
Why: shell is fragile and hard to test.
Where: docs/shell-to-python.md (maintainer only, not in the public edition).

### D99 - The repo keeps a complete architecture doc set
Status: built.
One overview plus one page per component. A change that alters a component's behaviour updates its page in the same change.
Why: anyone must be able to understand how Capsule works.
Where: `docs/architecture/`, maintenance/docs_sync.py (maintainer only, not in the public edition).

### D113 - Two-way Slack decisions
Status: built.
Capsule posts each question in Slack with a recommendation and accepts button or typed answers. The answer is stored as a decision and acted on. Deletes and rule changes cannot be answered from Slack. A merge still needs an explicit yes.
Why: the maintainer does not depend on one long-lived terminal chat.
Where: `capsule/slack_answer.py`, `capsule/slack.py`.

### D125 - Protect the code that trusts Slack answers and the container runner
Status: built.
These files join the protected list, like the landing code, so workers cannot change them.
Why: they decide what counts as approval and whether a change waits or fails.
Where: `capsule/workspace.py` (`PROTECTED`).

### D139 - Five review rounds, then a human
Status: built.
Review-and-fix attempts on a card are capped at 5 rounds. The optional escalation ladder (off by default) keeps its 6 tries. See also row 642.
Why: a card that is not getting fixed needs a person, not more rounds.
Where: `capsule/runaway.py` (`owner_rounds`), `capsule/restart.py`.

### D161 - Code-blind split for builds
Status: built.
Builders write code only. A separate blind tester writes tests from a behaviour spec in a private repo. The reviewer reads code but never the blind tests. A referee sees both only when a blind test fails.
Why: tests written by the author of the code only confirm the code.
Where: `capsule/runners/host_wall.py`, `capsule/blind/`, `docs/architecture/components/blind-path.md`.

### D162 - Finish the blind path from the approved plan
Status: built.
Interface snapshot, tester dispatch, red check, gate mode, mount audit, host wall hook, lander re-run and mutation score were built from the existing plan without a new planning round.
Why: the design was already agreed.
Where: `capsule/blind/`, `capsule/base_red.py`, `capsule/lander_blind.py`.

### D195 - Docker runs only while something needs it
Status: built.
Docker is used for landing checks and container tests, and is quit when idle.
Why: Docker Desktop holds several GB of memory, and Capsule must stay lightweight.
Where: `maintenance/docker_idle.py`.

### D198, D202 - The earlier agent runtime is removed
Status: built.
Its code, its deploy branches, its checks and its tests were removed. The factory in `capsule/` does not depend on it.
Why: nothing used the code any more.
Where: `capsule/legacy_names.py` lists where old names may still appear in history.

### D204 - Automatic disk cleanup with fixed rules
Status: built.
A script runs every 4 hours, uses no model, and acts only when free disk is under 60 GB. It removes only things that rebuild themselves or are provably finished (unused Capsule images, build cache, download caches, old test scratch copies, finished work folders whose code is on `main`). Every removal is logged.
Why: a full disk stops landings.
Where: `maintenance/disk_relief.py`.

### D215 - The secret-file read guard blocks by default
Status: built.
Block mode is the default. Warn mode is an explicit setting. An empty or unknown value means block.
Why: warn-only was a trial period (D66).
Where: bin/secret-read-guard.py (maintainer only, not in the public edition), `docs/architecture/components/secret-read-guard.md`.

### D220 - The reviewer audits each acceptance row
Status: built.
`review_verdict.v1` has an optional `contract_audit` list: one entry per numbered acceptance row, with met yes/no/unproven and evidence. A "no" is a design-fidelity finding. The check is soft and adds notes only.
Why: a reviewer should check the whole contract, not a sample.
Where: `capsule/contract_audit.py`.

### D222 - Checksum-only release workflow
Status: built.
A `v*` tag or a manual run builds the public portable edition tarball with SHA256 checksums. A provenance step switches on when the repo is public.
Why: users can verify what they download.
Where: `.github/workflows/release.yml`.

### D225 - Slack Socket Mode listener, intake only
Status: built.
A small listener in its own launchd service acks Slack button and reply events and hands every event to `python -m capsule.slack_answer event`, which decides what counts as an answer. The listener does not check the clicker or approve anything itself. Deletes and rule changes are never answered from Slack.
Why: the listener holds a token, so it must do as little as possible.
Where: `capsule/slack_listen.py`, `capsule/slack_secret.py`.

### D227 - The Slack token can come from the macOS Keychain
Status: built.
Only that key has a Keychain backend in `bin/capsule-secret`, so alerts work while Docker (and the secrets service) is down. A miss falls through to the secrets service.
Why: alerts must work during an outage.
Where: `bin/capsule-secret`, `capsule/slack_secret.py`.

### D229, D232 - Brainstorms run in Slack threads
Status: built.
Slack is the one place to talk to Capsule. Both the maintainer and Capsule may open a brainstorm. Replies use a mid-size model and are capped per day, and the brainstorm may search the notes vault read-only.
Why: one conversation channel, with a bounded cost.
Where: `capsule/brainstorm.py`, `capsule/brainstorm_run.py`.

### D234 - Weekly usage meter and pause
Status: built.
Weekly usage is estimated from local agent transcripts (token counts only, never message text) against a limit that the maintainer sets. At the pause line (`usage_pause_at`, default 0.90) the tick starts no new runs. Runs in flight, reviews and landings go on.
Why: stay inside the plan's weekly limit.
Where: `capsule/usage_meter.py`, `capsule/weekly_pause.py`.

### D238 - Merging the epic's pull request is the only approval
Status: built.
The old terminal approval path and its stamp code were retired. Readiness is bound to the pull request head. `maintenance/merge_gate.py` still holds the readers and the host-side writers for the spec and release approval files (`write_spec_approval`, `write_release_approval`).
Why: one approval path is easier to prove.
Where: `maintenance/merge_gate.py`, `capsule/publish.py`.

### D253 - Contributions are issues only
Status: built.
Anyone may open an issue (bug or idea). Code changes come only from the maintainer for now.
Why: keep the review load and the security surface small.
Where: `CONTRIBUTING.md`.

### D255 - Metrics stay on plain Postgres
Status: built.
Raw samples are kept 90 days. An hourly roll-up table is kept 2 years and feeds the weekly dashboards.
Why: the data is small, and a time-series extension would add a non-open licence and a database downgrade.
Where: `observability/postgres/migrations/009-samples-rollup.sql` (the roll-up table) and `maintenance/cleanup.sh` (the pruning); see `docs/RETENTION.md`. `maintenance/retention.sql` holds only the 180/30/14-day rules for the `events` table.

### D266 - Lightest option for every component
Status: built.
Choose the least resource-hungry option (memory, disk, services), so one laptop can run Capsule. Check each new service or dependency against this first.
Why: anyone should be able to run it, and it must still scale.
Where: `docs/PRODUCT.md`, `docs/architecture/overview.md`.

### D270 - Retired names stay only in stored history
Status: built.
Old names may remain in stored history (old database rows, dashboard filters for that period, old run ids). Live code and docs use the current names. No data migration.
Why: rewriting history costs more than it gives.
Where: `capsule/legacy_names.py`.

### D273 - Landing gates run the tests the change needs
Status: built.
A landing runs only the tests selected for the changed parts. The full suite and the blind suites run once a night. Selection never skips the blind gate. Off unless a board sets `lander.select_tests`.
Why: full-suite landings take hours.
Where: `capsule/lander_select.py`, `tests/select_tests.py`.

### D305 - A Mac lane
Status: built.
A `mac` runner tier runs one job at a time on the whole host (a lock). The sandbox is the light tier's profile, loosened only for the Swift toolchain and GPU rendering. It writes only inside the job's folder, allows localhost only, and is switched on per board.
Why: Mac app builds need the host, not a Linux container.
Where: `capsule/runners/mac.py`, `capsule/runners/seatbelt.py`.

### D328 - Worker image precedence: most specific wins
Status: built.
Order: `OPS_WORKER_IMAGE` in the process environment, then the board's own project image row, then `OPS_WORKER_IMAGE` in the machine-wide env file, then the default Python image.
Why: a machine-wide pin must not silently override every project's own image.
Where: `capsule/runners/docker.py` (`worker_image_source`).

### D335 - The stuck-card alarm needs the same class on the same file
Status: built.
A same-class streak counts only when the same finding class repeats on the same file in each round of the window.
Why: a card fixing a different file each round is not stuck.
Where: `capsule/runaway.py`.

### D338, D339 - Stall supervisor and unattended running
Status: built.
Each stall class gets one record and one incident, then an escalation ladder: script remedies, one guide ruling, then a page to a person. A 30-minute patrol answers "why is nothing happening". Quiet stops default to continue (resume or re-dispatch with a note, then a stronger model, then set aside). Irreversible or safety holds are set aside and the factory routes around them.
Why: the factory should run for weeks without a person watching.
Where: `capsule/stall.py`, `capsule/stall_owner.py`, `capsule/patrol.py`, `docs/architecture/components/stall-supervisor.md`.

## Row decisions

### Row 21 - Observability as a role
Status: built.
A Lookout role writes each project's observability plan, provisions its dashboards from code, and reviews new service cards for metrics and logs.
Why: monitoring should be designed, not bolted on.
Where: `contracts/lookout.md`, `capsule/lookout.py`.

### Row 55 - The Lookout derives a contract; scripts do the rest
Status: built.
A model role reads an application's design and code and writes its observability contract (signals, SLOs, alerts, dashboards) as data. Plain scripts generate dashboards from it, lint it at the gate, probe it and check it is not stale.
Why: split the judgement (model) from the repeatable work (scripts).
Where: `capsule/lookout.py`, `capsule/lookout_ship.py`, `docs/observability-conventions.md`.

### Row 64 - When the Lookout runs
Status: built.
At milestones (first deploy, new service, new dependency), weekly, and on demand. It proposes and never applies. A person approves, then scripts apply.
Why: observability changes are reviewed like code.
Where: `contracts/lookout.md`.

### Row 75 - Written retention and egress policy per store
Status: built.
Each store has a written retention period with a reason, a retention job, and secret scrubbing on ingestion. One written rule says what may leave the machine.
Why: data should not live or travel further than needed.
Where: `docs/RETENTION.md`, `docs/EGRESS.md`, `maintenance/scrub.py`.

### Row 108 - Code-writing workers run with no network
Status: built.
The code roles run with `network: none`. Pushes happen on the host after the run. A card that needs a new package fails clearly and becomes an image-rebuild card.
Why: a worker that cannot reach the network cannot leak or fetch anything.
Where: `capsule/runners/docker.py`.

### Row 125 - Partial backups exit 1; the reviewer gets a host-made diff
Status: built.
A backup that misses one dump exits 1 but keeps the tarball. The reviewer has no shell, so the host writes the review diff packet for it.
Why: A partial backup is still worth more than none, but it must not report success. A role that cannot run commands needs the diff handed to it.
Where: `maintenance/backup.sh`, `maintenance/board_bridge.py`.

### Row 127 - The verdict record is host-only
Status: built.
With `OPS_VERDICT_STORE` on, the board comment is only a mirror of a verdict record that lives on the host. The committer rule for vault notes is proved by a fixture test.
Why: Any agent that can edit a board comment could change a verdict written there.
Where: `maintenance/contracts.py`, `maintenance/verdicts.py`, `maintenance/vault_jobs.py`.

### Row 128 - Vault notes are pushed per role, never mounted
Status: built.
The optional route key `vault_read` in the routing config says which vault notes dispatch may push into a role's brief. A missing key means none. No role mounts the vault. See also rows 386 and 626.
Why: A sandboxed worker with a search tool can leak or be steered.
Where: `capsule/config.py`, `docs/architecture/components/vault-context.md`.

### Row 132 - The retrieval floor is stored per case set
Status: built.
The monthly retrieval eval keeps its real floor per version of the case set. When a run falls below it, the eval writes `last_regression` into the state file and the daily report shows it.
Why: A new case set must not be judged against the floor of an older one.
Where: `maintenance/retrieval_eval_job.py`, `maintenance/report.sh`.

### Row 136 - The daily brief v2
Status: built.
The brief job (`report.sh` and `brief.py`) puts the waiting block first, folds in the overnight section and ends with a closing line. It is switched on by `OPS_BRIEF_V2`, which `run.sh` exports to the child processes.
Why: The reader should see what waits on a person before anything else.
Where: `maintenance/brief.py`, `maintenance/report.sh`, `maintenance/run.sh`.

### Row 137 - Backups are encrypted before they leave the machine
Status: built.
`backup_enc()` encrypts the backup tarball on the laptop, behind `OPS_BACKUP_ENCRYPT`. `restore.sh` decrypts it first. The off-machine copy itself is a manual step by the maintainer, so scripts never do it.
Why: Nothing readable should be able to leave the machine.
Where: `maintenance/backup_key.sh`, `maintenance/restore.sh`, `docs/EGRESS.md`.

### Row 138 - Release events have one emitter
Status: built.
The `release` event (repo, version) is emitted only by the release gate. A repo-conventions template carries the rulings that every new repo inherits, so they are not decided again.
Why: One source for release facts, and no repeated debates.
Where: `maintenance/events.py`, `maintenance/release_gate.py`, `maintenance/templates/repo-conventions.md`.

### Row 139 - Scheduled upkeep cards and Renovate
Status: built.
A quarterly "what do we remove?" card and a Renovate card for repos that Capsule does not write to are filed by plain scripts through `capsule intake`. Renovate runs weekly, grouped, with patch updates merged on green CI (see D239).
Why: Upkeep must happen on a schedule without a model call.
Where: `maintenance/run.sh`, `maintenance/templates/renovate-card.md`.

### Row 140 - SBOM and vulnerability scan
Status: built.
`sbom.sh` writes an SBOM in SPDX and CycloneDX and runs a vulnerability scan. Each tool is optional, and a missing tool prints a skip line. A weekly deep licence scan runs after the image push, report-only, so a finding never blocks an image.
Why: A missing scanner must never look like a clean result, and a report must not stop a build.
Where: `maintenance/sbom.sh`, `.github/workflows/image.yml`.

### Row 201 - Caps are enforced in code, not in the prompt
Status: built.
A planning job's card cap is clamped in `run.sh` before the session sees the number (`SPRINT_MAX_CARDS`), so a drifting prompt cannot ask for more. The probe also records the size of the vault-note block that a card carried.
Why: A limit that lives only in a prompt can be talked past.
Where: `maintenance/run.sh`, `maintenance/obs_probe.py`, `maintenance/report.sh`.

### Row 208 - Pause times show the weekday
Status: built.
A bare clock time that crosses midnight read as already past, so the brief and the report now carry the weekday with a pause time.
Why: A pause that ends tomorrow must not read as over.
Where: `maintenance/brief.py`, `maintenance/run.sh`.

### Row 217 - Dependencies are synced on the host
Status: built.
`deps_sync.py` fills a dependency cache on the host, which has network, so a worker with no network can still build.
Why: Workers run with no network (row 108).
Where: `maintenance/deps_sync.py`, `maintenance/run.sh`.

### Row 352 - Code controls, prompts only judge
Status: built.
Deterministic code owns control (tick loop, gates, hand-off checks, escalation, budgets). Prompts own judgement only (decomposition, review, design taste). A model swap means re-testing the judgement prompts against golden tasks and nothing else.
Why: control logic in prompts grows with every incident and costs more.
Where: `capsule/tick.py`, `capsule/golden.py`, `docs/architecture/overview.md`.

### Row 356 - A validation contract per task
Status: built.
Every task carries acceptance assertions written before the code (commands and expected results, golden files). A validator that never sees the diff runs them against the built result.
Why: "done" must be checkable and independent of the code.
Where: `contracts/validator.md`, `capsule/validator_report.py`.

### Row 357 - The Docker reaper touches only containers Capsule made
Status: built.
A container with an empty or mismatched run-id label, or a name Capsule did not mint, is never touched, and a live run is never reaped. In the light tier, scratch space is a relative folder under the worktree.
Why: A reap sweep once threatened containers that were not Capsule's.
Where: `capsule/runners/docker.py`, `capsule/runners/seatbelt.py`.

### Row 361 - Re-alignment trigger
Status: built.
A change to a spec section, a public interface or a validation contract stops the worker or reviewer, which raises it. The epic pauses until a person rules. Patches are allowed only inside a task's own scope.
Why: drift in what was agreed must be a human call.
Where: `contracts/guide.md`, `capsule/amend.py`.

### Row 362 - Which reviews block a merge
Status: built.
Blocking: the code-blind validator, the code review, the security review and the alignment review. Advisory (shown, never blocking): lint, mutation, licence, SBOM, policy. A review that passes while listing findings is refused.
Why: block on correctness and safety; inform on the rest.
Where: `capsule/tick_review.py`, `capsule/review_findings.py`.

### Row 363 - Three validator roles
Status: built.
The reviewer reads the diff. A code-blind validator runs the contract against the built artefact. On UI work the validator also drives the app.
Why: each role catches a different kind of mistake.
Where: `contracts/reviewer.md`, `contracts/validator.md`.

### Row 364 - The guide reads the rows a card cites
Status: built.
Each decision row that a card or an epic entry cites is inlined whole into the guide's brief, because a role with only the numbers cannot name a row.
Why: The guide cannot search the vault, so it judges only what it is handed.
Where: `capsule/bundle_sections.py`, `capsule/config.py`.

### Row 365 - Reviewer and validator output are schema documents
Status: built.
The reviewer returns `review_verdict.v1` and the validator returns `validator_result.v1`; both are checked against schemas in the repo. See rows 362, 363 and 416.
Why: A verdict must be machine-checkable.
Where: `contracts/reviewer.md`, `contracts/schemas/validator_result.v1.json`.

### Row 366 - Every role has a contract
Status: built.
A contract lists the inputs the role gets, what "done" means for it (checkable), what it may see, and what it must report. Roles are code-blind unless their contract says otherwise.
Why: short, checkable contracts replace long rule files.
Where: `contracts/`.

### Row 369 - A UI epic stops after its first screen
Status: built.
When the first screen of a UI epic is built, the epic is held until the maintainer answers `screen-ok` or `screen-fix`.
Why: A wrong look is cheaper to fix on screen one than after the whole epic.
Where: `capsule/blocks.py` (`hold_first_screen`), `capsule/epic_sync.py`, `capsule/__main__.py`.

### Row 373 - Light runner and strong runner
Status: built.
The light tier is a git worktree plus a macOS seatbelt profile that allows only that worktree. The strong tier is a hardened Docker container. Guardrails are three loop counters.
Why: pick isolation by risk.
Where: `capsule/runners/seatbelt.py`, `capsule/runners/docker.py`.

### Row 376 - One SQLite file per board
Status: built.
WAL mode, one writer (the tick), holding cards, runs, leases, a resettable failure counter and the message outbox. Agents never write it directly.
Why: one authoritative store instead of five formats.
Where: `capsule/db.py`, `capsule/board_schema.sql`, `capsule/tick_guard.py`.

### Row 377 - Mechanical fixes to measured faults
Status: built.
The tick re-reads caps and config every tick, the event log rotates daily with a cap, worker containers never persist across runs and a reap sweep runs each tick.
Why: each fixed a measured fault.
Where: `capsule/tick.py`, `capsule/log_caps.py`.

### Row 378 - Sandbox tier by risk class
Status: built.
Default light. Strong (container, no network) for cards that install dependencies, run browsers or touch the network. A guide may raise a tier, never lower it.
Why: spend isolation where risk is.
Where: `capsule/risk.py`, `capsule/runners/`.

### Row 381 - Model per role, in one config file
Status: built.
Planning, alignment, guide and security review use the strong model; implementers a mid-size model; the code-blind validator a small model with a fallback. The table is re-read every tick.
Why: spend model cost where judgement matters.
Where: `capsule/config.py` (`routing`).

### Row 384 - Contract and acceptance criteria are never truncated
Status: built.
They are inlined whole. Retrieval fills only the remaining budget.
Why: a truncated acceptance criterion caused a real failure.
Where: `capsule/bundles.py`.

### Row 386 - Workers get pushed context, no live search
Status: built.
Nothing that writes code can search the vault or the network. Host-side judge roles may search.
Why: a sandboxed worker with a search tool can leak or be steered.
Where: `capsule/vault_context.py`.

### Row 391 - The interventions-per-card report
Status: built.
`capsule report <board>` counts how often a person had to step in per card and compares the rate with an earlier baseline (U1).
Why: It is the main measure of how well the factory runs alone.
Where: `capsule/report.py`, `capsule/__main__.py`, `capsule/db.py`.

### Row 393 - Prompt and contract files need an approval stamp
Status: built.
Contracts and role prompts live in the repo, and a change needs the maintainer's approval stamp. Rules are scoped per role. Every added rule must name what it removes or why nothing can go.
Why: prompt drift is the main way a factory weakens itself.
Where: `contracts/SHA256SUMS`, `capsule/contracts.py`, `docs/ZENKAI.md`.

### Row 394 - The introspection pass
Status: built.
A weekly pass reads run history (rework, wasted runs, review findings by class, incidents) and proposes prunes and golden-set additions as cards. Each needs the row 393 approval.
Why: the factory learns from its own failures without editing itself.
Where: `contracts/auditor.md`, `capsule/auditor.py`.

### Row 395 - No worker may write a contract, role prompt or instruction file
Status: built.
Three layers: the files are not writable inside any runner, a deny hook refuses edits for every profile, and the landing gate refuses any diff touching them. A failing round says `protected path changed (row 395)` and restores the file.
Why: an agent that can edit its own rules can remove them.
Where: `capsule/workspace.py` (`PROTECTED`), `capsule/protected_scope.py`, `capsule/claims.py`.

### Row 400 - Serial inside an epic
Status: built.
One task at a time on one branch, each worker inheriting the last through git. Different epics run in parallel only when their plans declare disjoint file sets. Read-only work runs in parallel freely.
Why: parallel work on the same files caused real incidents.
Where: `capsule/epic_graph.py`, `capsule/dispatch.py`.

### Row 416 - Every hand-off is a schema-checked document
Status: built.
Anything that crosses a role boundary (worker report, review verdict, validator result, decision) is JSON checked against a schema kept in the repo and versioned with the contracts.
Why: regex-parsed text failed in practice.
Where: `contracts/schemas/`, `capsule/jsonschema.py`.

### Row 417 - Checkpoint and restart, never summarising
Status: built.
A worker that runs out of turn budget returns `status: checkpoint` with a checkpoint object. The next run restarts from it. Nothing is summarised in context.
Why: A summary loses detail, and a checkpoint is checkable.
Where: `capsule/checkpoint.py`, `contracts/implementer.md`, `capsule/models/__init__.py`.

### Row 420 - Agents never write the board; they drop files in a spool
Status: built.
A worker drops request and report files in a per-run spool. The tick validates and applies them on the next tick.
Why: one writer per board keeps state consistent and untrusted output contained.
Where: `capsule/spool.py`.

### Row 424, Row 425, Row 637 - Slack message classes and glyphs
Status: built.
Each message class has a glyph, and the first matching prefix wins. A `decision.v1` document shows as one line that names the card, the recommended option and the reply to send. `ask` and `page` messages go to the inbox channel, then the `needs_input` channel, then the `blocked` channel, so a page is never lost. A resolved alert keeps its class glyph. This entry covers rows 424, 425 and 637.
Why: The class must be clear at a glance, and a page must always arrive.
Where: `capsule/slack.py`, `capsule/spool.py`, `observability/grafana/provisioning/alerting/contact-points-slack.yaml.template`.

### Row 447 - Review-stage fixes: no `not_run` validator documents, no unchecked findings
Status: built.
A validator document may not name any assertion `not_run`. A `pass` with non-empty findings (row 362) and a late non-safety `fail` (row 540) are refused as verdicts.
Why: a verdict that did not run its checks must not count as a content failure.
Where: `capsule/validator_report.py`, `capsule/review_findings.py`.

### Row 452 - A human `amend` verb
Status: built.
`capsule human ... amend` edits a dispatched card's body or file set, and it backs the board up first. `CAPSULE_BACKUP_DIR` moves the backup so tests never touch the real one.
Why: A one-off manual fix became a checked verb.
Where: `capsule/__main__.py`, `capsule/paths.py`.

### Row 454, Row 455 - The Zenkai Rules
Status: built.
Every contract or script gets intent examples (real inputs plus the outcome a human calls right); a contract pass with an intent-example fail is a contradiction event. Every check has two independent oracles. The rules are written as an obligation, a named enforcer and what counts as a violation; anything not yet mechanised is marked "enforced by review until mechanised".
Why: a script can be reliably wrong; wording alone cannot close loopholes, enforcement can.
Where: `docs/ZENKAI.md`, `contracts/intent/`, `capsule/intent.py`, `capsule/contradiction.py`.

### Row 456 - Korin: incidents as a role, a record and a gate
Status: built.
Every crash, hang, silent failure, contradiction, human intervention, override or repeat opens an incident record automatically. A post-mortem names root cause, lesson, mitigation, an enforcer and a recurrence test. The incident closes only through the gate, and a repeat of a closed incident is the headline number.
Why: the factory must come back stronger from each failure.
Where: `capsule/incidents.py`, `contracts/korin.md`, `docs/architecture/components/incidents-korin-flow.md`.

### Row 459 - Test-honesty gate before every merge
Status: built.
New or changed tests get an independent audit: static checks (asserts nothing, mocks the subject, tautology, swallowed exceptions) and mutation checks (revert the production change and the tests must fail).
Why: green tests that cannot fail prove nothing.
Where: `tests/mutation_check.py`, `capsule/hollow_tests.py`.

### Row 465 - An implementer edits only its assigned worktree
Status: built.
An implementer once edited the main checkout. Now the brief carries a "Before you edit" note with the assigned workspace path, the host preflight refuses a mismatched run, and the crash class is keyed by `error_class`, so `worktree_dirty` and `worktree_mismatch` repeats do not share a fingerprint.
Why: Writing outside the assigned worktree can damage shared state.
Where: `capsule/worker.py`, `capsule/bundles.py` (`WORKTREE_NOTE`), `capsule/incidents.py`.

### Row 470 - Branch owners and the unblocked hook
Status: built.
`capsule unblocked` reads branch owners from `$CAPSULE_HOME/queue/<repo>/owners.json`, never from a file in the repo. An owner counts only while its heartbeat is under 40 minutes old. `bin/capsule-own` claims or releases a branch. The stop hook acts only inside the allowed repos and does nothing when the working directory is empty or unknown (fail closed).
Why: An agent that goes quiet must fall back to unowned, and a hook must not act on a session it cannot place.
Where: `bin/capsule-own`, `bin/capsule-unblocked-hook.sh`, `capsule/unblocked.py`.

### Row 472 - A stale spec accepts or resets, never reruns
Status: built.
A `spec_stale` block can only be `accept` or `reset`; unblocking is refused. Such holds are counted apart from failures in the report.
Why: The card's work is not what is wrong, the spec is.
Where: `capsule/blocks.py`, `capsule/report.py`.

### Row 476, Row 478, Row 480 - The Lookout: operator dashboards
Status: built.
A factory-floor home dashboard shows every live card with product, epic, role, model, elapsed time and what it waits on. Producers write `cap_*` tables to Postgres, Capsule logs go to Loki, and the work was built in tasks with an undo per migration step.
Why: the earlier dashboards read frozen data and did not show what was running.
Where: `capsule/lookout.py`, `capsule/lookout_ship.py`, `docs/architecture/components/observability.md`.

### Row 507 - Migration check and repeated invalid documents
Status: built.
`db.py` has a read-only check that tells whether `migrate` would still add a column or table or widen `incidents.kind`; the tick startup and `capsule check` share it. A review role whose own document was a repeated invalid `requests/` document is its own incident cause, and the next brief of a retried role names the `invalid_document_error`.
Why: One oracle for schema state, and a clear cause for a role that cannot write a valid document.
Where: `capsule/db.py`, `capsule/invalid_doc.py`, `capsule/incidents.py`.

### Row 509 - Simplicity rules (Zenkai Z7)
Status: built.
Every implementer report carries a net production-lines figure and, above +150 lines, an answer to "what could have been deleted". A monthly simplification card rewrites the worst baseline entries. Size baselines can only get smaller.
Why: a code audit found growing complexity with no transition table for card status.
Where: `tests/size_check.py`, `tests/size_baseline.json`, `docs/architecture/components/gate-simplicity-and-size.md`.

### Row 540 - From review round 4, only ACCEPT or a decision
Status: built.
From the 4th review round the reviewer returns ACCEPT or a decision for the maintainer. A new finding is allowed only as a regression; everything else goes to non-blocking notes.
Why: endless new findings stop a card from finishing.
Where: `contracts/reviewer.md`, `capsule/review_findings.py`.

### Row 558 - Token spend and the builder pair gate
Status: built.
`capsule tokens` prints subagent token spend per card, role and outcome for one day, read-only over agent transcripts. The unblocked stop hook runs its pair gate only with a working detector report and while a seat is free.
Why: Cost must be visible, and a broken detector must not block work.
Where: `capsule/token_accountant.py`, `capsule/unblocked_stop.py`.

### Row 568 - Never delete, weaken or move a guard test to satisfy a gate
Status: built.
The mutation gate's two heuristics became info-level findings with a reason. The rule is in the shared rules and the implementer contract. A weakened or deleted test is a reviewer finding.
Why: an implementer must fix the code, not the test.
Where: `contracts/implementer.md`, `tests/mutation_info.py`.

### Row 569 - Schemas for every agent output
Status: built.
Schemas were pinned for the task-event payloads, intervention records, spool rows, unblocked items, outbox rows and the typed intent `source` field. Changing a schema needs a decision row.
Why: unschema'd output is where silent drift starts.
Where: `contracts/schemas/`, `capsule/row_schemas.py`, `capsule/db.py`.

### Row 570, Row 573 - A shared style checklist for contracts
Status: built.
The contract loader appends one shared style checklist to every role contract, and preflight lints it. This entry covers rows 570 and 573.
Why: Short, uniform contracts are easier to check.
Where: `capsule/contracts.py`, `capsule/preflight.py`, `capsule/style_lint.py`.

### Row 584 - Cost of a card is measured by the host
Status: built.
Cost is the sum over all runs (every role, every attempt) of fresh tokens and wall-clock seconds. The host measures it. A worker never reports its own cost. There is no dollar figure.
Why: self-reported cost cannot be trusted, and the factory is subscription-billed.
Where: `capsule/cost.py`, `capsule/token_accountant.py`.

### Row 600 - Landing rules
Status: built.
The lander lands each card on the harness repo's `main`. Product boards land through one daily pull request that a person merges. Protected paths still need the row 393 stamp. The lander starts in dry-run until proven.
Why: harness changes are small and checked; product changes get a human merge.
Where: `capsule/lander.py`, `capsule/daily_pr.py`.

### Row 601 - Two test layers
Status: built.
Acceptance tests live in their own repo, written code-blind from the behaviour contract, and only results flow back. Implementer unit tests stay and the mutation gate keeps them honest. Reviewers may read implementer test changes.
Why: independent acceptance tests, plus honest unit tests.
Where: `capsule/blind/`, `docs/architecture/components/blind-path.md`.

### Row 602 - Loophole hunt before a contract change
Status: built.
Every contract, intent-example or schema change gets a hunt by a fresh cheap agent that tries to satisfy the wording while breaking the intent. Each loophole becomes an intent example or a gate rule in the same change. A deterministic pre-check lists cheap loopholes.
Why: close loopholes with executable examples, not more English.
Where: `capsule/loophole.py`, `contracts/intent/`.

### Row 609 - One rule for `CAPSULE_HOME`
Status: built.
An explicit value wins. Under `AGENT_OPS_NO_SEND`, scripts use a temp directory and never the live home. The same block is in every script that defaults it, matching `home()` in `paths.py`.
Why: A test or dry run must never touch live state.
Where: `capsule/paths.py`, `bin/capsule-own`, `bin/capsule-keeper-watch.sh`.

### Row 616, Row 617 - Eval incidents open the report
Status: built.
The `EVAL INCIDENT` lines open the report document, followed by `## Eval checks`. The bar for owner touches per card is 1.92 (row 616).
Why: A failing eval must be the first thing a reader sees.
Where: `capsule/report.py`, `capsule/touches.py`.

### Row 624 - The tick cleans a dirty epic clone
Status: built.
If the epic clone has uncommitted changes and no card holds it, the tick saves the full diff to a dated patch outside the clone, verifies it, and only then resets to `origin/main`. A failed backup discards nothing.
Why: replace a manual reset that people did by hand.
Where: `capsule/epic_clean.py`.

### Row 626 - Vault context for workers
Status: built.
At dispatch, the tick retrieves the top 3 decision rows and top 3 notes for a card and attaches them to the implementer, reviewer and guide briefs with provenance, within 1,200 tokens. Workers never search the vault and get no vault mount.
Why: better alignment without giving a sandboxed worker a search tool.
Where: `capsule/vault_context.py`.

### Row 632 - Host-exec hardening
Status: built.
The lander gate scripts, the size baseline and deploy scripts join the protected list. Worker tests run inside the locked-down container, not on the host.
Why: a gate that a worker can edit is not a gate.
Where: `capsule/workspace.py` (`PROTECTED`).

### Row 633 - A read-only board query for a review job
Status: built.
`board_query.py` runs a read-only SELECT over one board's database. `run.sh --restricted` fences Read to the vault, so the planner cannot open `ops.env`.
Why: A planning job needs board facts and nothing else.
Where: `maintenance/board_query.py`, `maintenance/run.sh`.

### Row 638 - Old incidents close as history
Status: built.
The history closer lists incidents whose issue is no longer open, each with a verdict and one line of evidence, and closes them with closure kind `history`, without the Korin gate. The incident counts report them apart from repairs.
Why: Old incidents should not stay open forever, and they must not count as repairs.
Where: `capsule/incident_history.py`, `capsule/incidents.py`.

### Row 642 - Five review rounds, then the owner decides
Status: built.
A card that fails its 5th review round since the last human reset stops and asks a person in plain words (restart with the findings, drop and refile smaller, or narrow). Late-round rulings do not reset the count; a human reset does.
Why: a card that does not get fixed needs a person.
Where: `capsule/runaway.py` (`owner_rounds`).

### Row 643 - Common maintenance is scripted, never improvised
Status: built.
Recurring cleanup and landing chores are tested Python automations that preview first, touch only what Capsule created and labelled, log every run and are safe to repeat (branch and work-folder sweep, Docker and temp storage, and others).
Why: improvised cleanup is how things get deleted by mistake.
Where: `maintenance/branch_sweep.py`, `maintenance/storage_gc.py`, `maintenance/disk_relief.py`.

### Row 646 - Incidents dedupe in a rolling 24 hours
Status: built.
An event attaches to a live incident with the same fingerprint when that incident opened less than 24 hours before. The window was the UTC calendar day; now an outage that crosses 00:00 UTC is one incident. An event exactly 24 hours later opens a new incident. Nothing else changes: an incident with a fix pending takes later events on any day, human interventions and overrides never dedupe, a `watch` closure is left out, and a fingerprint closed in the last 30 days opens a `repeat`. The change is reversible: one query, no schema change and no stored state.
Why: the calendar day was a first-version rate limit, not a decided rule. It split one outage into two incidents at 00:00 UTC, and each twin cost one more Korin run, fix card and gate pass. Attaching to any open incident on any day was rejected: an old incident that nobody works would hide new failures. With 24 hours, an unworked incident holds at most one day of events and then a new one opens.
Where: `capsule/incidents.py`, `capsule/tests/test_incident_rolling_window.py`, `docs/architecture/components/incidents-korin-flow.md`.

## Ids that are not published

Every id below is cited in the docs or in public code, and none has an entry above. They are rules about the maintainer's own working routine, internal records of planning sessions, one-off incident notes, product-specific work, or ids that were replaced. They are not written up here.

| D id | Status |
|---|---|
| D3, D13, D21, D26, D30, D39, D44, D59 | internal record or rule, not published |
| D65, D106, D132, D133, D153, D154, D155, D159 | internal record or rule, not published |
| D167, D170, D172, D190, D206, D209, D210, D212 | internal record or rule, not published |
| D221, D224, D226, D239, D240, D243, D244, D251 | internal record or rule, not published |
| D254, D260, D261, D269, D312, D320, D329 | internal record or rule, not published |

| Row | Status |
|---|---|
| 9, 12, 20, 23, 26, 27, 30, 32 | internal record, not published |
| 35, 39, 40, 42, 44, 46, 50, 51 | internal record, not published |
| 63, 66, 69, 73, 82, 83, 84, 85 | internal record, not published |
| 87, 97, 99, 106, 109, 110, 113, 114 | internal record, not published |
| 117, 118, 119, 120, 121, 123, 124, 126 | internal record, not published |
| 129, 131, 133, 135, 142, 153, 156, 164 | internal record, not published |
| 171, 210, 251, 291, 305, 309, 335, 341 | internal record, not published |
| 347, 349, 351, 354, 355, 359, 360, 367 | internal record, not published |
| 368, 370, 379, 380, 385, 389, 396, 397 | internal record, not published |
| 398, 403, 414, 418, 419, 421, 440, 445 | internal record, not published |
| 449, 450, 457, 460, 466, 468, 469, 473 | internal record, not published |
| 474, 481, 482, 483, 485, 487, 491, 492 | internal record, not published |
| 496, 503, 506, 508, 511, 512, 514, 515 | internal record, not published |
| 517, 519, 520, 526, 529, 530, 537, 541 | internal record, not published |
| 543, 545, 550, 551, 553, 557, 559, 563 | internal record, not published |
| 566, 567, 571, 572, 575, 576, 577, 579 | internal record, not published |
| 580, 581, 583, 585, 587, 590, 591, 592 | internal record, not published |
| 593, 594, 595, 596, 597, 598, 599, 603 | internal record, not published |
| 604, 605, 607, 610, 611, 612, 613, 615 | internal record, not published |
| 621, 623, 629, 635, 636, 639, 644, 645 | internal record, not published |
