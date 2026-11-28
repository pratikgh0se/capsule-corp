# Capsule architecture documentation

Decision D99 (2026-10-28): Capsule keeps a complete, current architecture doc set in the repo, so
anyone can understand how it works. This directory is that set. It describes `main` at commit
`f1f54a228` (the overview, planned list, inventory and the secret and public-edition pages were re-audited 2026-11-27; other pages carry their own audit); every claim names the file that proves it.

## Start here

| Page | What it is |
|---|---|
| [overview.md](overview.md) | High-level design: purpose, the loop, component map, data stores, trust boundaries, external services, deployment. |
| [components/](components/) | One low-level page per component (roles, mechanisms, gates, services). Index below. |
| [inventory.md](inventory.md) | Every `capsule/*.py` module, maintenance script, `bin/` script and launchd job, mapped to its component page. |
| [glossary.md](glossary.md) | Plain definitions of the words Capsule uses (card, tick, seat, incident and more). |
| [data-model.md](data-model.md) | The tables, files and documents that hold Capsule's state. |
| [walkthrough.md](walkthrough.md) | One card followed from intake to landing. |
| [extending.md](extending.md) | How to add a role, a gate or a check. |
| [planned.md](planned.md) | Decided but unbuilt designs, with pointers to their specs. |

Older and related docs (not replaced by this set): [../ARCHITECTURE.md](../ARCHITECTURE.md) is the
one-page map (it links back here); the operating steps live in the component pages (each covers how its part runs and fails); [../ZENKAI.md](../ZENKAI.md) is the reliability doctrine;
[../SETUP.md](../SETUP.md) is install; the design specs and decision rows that the Sources lines cite are internal notes and do not ship.

## Component index

Every component page uses one template: Purpose; Where it lives (files); Inputs and outputs; State
it reads and writes; Key rules and invariants; Failure modes and what happens; Tests that pin it;
Related decisions and specs; Status (current, partial or planned).

**Core machinery**

- [tick-scheduler](components/tick-scheduler.md): the tick loop, single-writer lock, config, boundary restart, preflight, invariants.
- [board-store](components/board-store.md): the SQLite board, the card state machine, events, spool, leases, checkpoints.
- [dispatch-and-caps](components/dispatch-and-caps.md): which card may start, seats, caps, epic clones, the unblocked detector.
- [worker-runner-and-images](components/worker-runner-and-images.md): one model run, prompt bundles, runner tiers, workspaces, worker images.
- [image-record](components/image-record.md): per-board image record, `image render|build|which`, resolution order.
- [blocks-and-stop-the-line](components/blocks-and-stop-the-line.md): block router, runaway stop, restart ladder, contradictions, amend and auto-amend.
- [vault-context](components/vault-context.md): decision rows and notes pushed into a worker's brief.
- [intake](components/intake.md): how cards, human verbs and cross-board files enter a board.
- [outbox-notifications](components/outbox-notifications.md): outbox, Slack classes, digest, quiet hours.

**Roles** (each has a contract in `contracts/`)

[implementer](components/role-implementer.md), [reviewer](components/role-reviewer.md),
[validator](components/role-validator.md), [guide](components/role-guide.md),
[blind tester](components/role-blind-tester.md), [Korin](components/role-korin.md),
[security reviewer](components/role-security-reviewer.md), [lookout](components/role-lookout.md),
[advisor](components/role-advisor.md), [auditor](components/role-auditor.md),
[security engineer](components/role-security-engineer.md),
[staff architect](components/role-staff-architect.md).

**Gates**

- [gate-intent-contracts](components/gate-intent-contracts.md): intent examples, schemas, `SHA256SUMS`.
- [gate-review-checks](components/gate-review-checks.md): in-tick review checks (review findings, validator rule, check_gate, style lint, override).
- [blind-path](components/blind-path.md): the whole blind path (tester, red check, run, host wall, lander re-run).
- [gate-hidden-tests](components/gate-hidden-tests.md): the blind gate (report and gate modes).
- [gate-mutation-check](components/gate-mutation-check.md): test-honesty merge gate.
- [gate-simplicity-and-size](components/gate-simplicity-and-size.md): Z8 simplicity check and Z7 size check.
- [gate-tamper-guard](components/gate-tamper-guard.md): test-tamper protection (contracts, mutation check, protected paths).
- [gate-protected-paths](components/gate-protected-paths.md): paths a worker may not change.

**Landing and incidents**

- [lander-and-keeper](components/lander-and-keeper.md): landing checks, push, revert memory, keeper watchdog.
- [product-board-landing](components/product-board-landing.md): day branch, daily PR, publish, rhythm.
- [detectors-z5](components/detectors-z5.md): docker down, exec outage, invariants.
- [incidents-korin-flow](components/incidents-korin-flow.md): the incident record, post-mortem, closure gate.

**Host services and operations**

- [deploy-and-drift](components/deploy-and-drift.md): bootstrap.sh (maintainer only, not in the public edition), deploy.sh (maintainer only, not in the public edition), drift check, tick control, hoi-poi, park.
- [compose-live-guard](components/compose-live-guard.md), [secret-read-guard](components/secret-read-guard.md), [secrets](components/secrets.md), [read-guard](components/read-guard.md) (the allow-list for worker reads).
- [maintenance-jobs](components/maintenance-jobs.md): launchd jobs and `maintenance/` scripts.
- [tick-board-plist](components/tick-board-plist.md), [slack-listen-job](components/slack-listen-job.md), [docker-idle-job](components/docker-idle-job.md): the per-board tick template and two install-only jobs.
- [observability-compose-stack](components/observability-compose-stack.md): the eight-service compose stack.
- [cos-tools](components/cos-tools.md): `capsule cos`, landing prep, after-landing chain, branch sweep, storage and disk cleanup.
- [public-edition](components/public-edition.md): `capsule doctor`, `capsule install`, the public export.
- [observability](components/observability.md): Lookout feed, probe, Grafana, Loki, status screens.
- [evals-and-report](components/evals-and-report.md): report, eval checks, outcomes, golden sets, cost and token accounting.
- [zenkai](components/zenkai.md): how the Zenkai Rules bind this repo.

## How this set is maintained

**Rule: a behaviour change updates its page in the same change.** A change that alters what a
component does, reads, writes, refuses or how it fails edits that component's page, and
`inventory.md` when it adds, renames or removes a module, script or job. A reviewer treats a
behaviour change with no page edit as a finding.

Conventions:

- Describe what the code does on `main`. If a design is decided but not built, put it in
  [planned.md](planned.md) and set the page `Status` to `planned` or `partial`; never describe an
  unbuilt thing as current.
- Name the file (and function where useful) behind each claim so a reader can check it.
- Where code and another doc disagree, trust the code, fix or flag the other doc, and say so on the
  page.
- Cite the Zenkai Rules by item (Z1 to Z8); never copy them (`docs/ZENKAI.md` is a checksummed
  mirror).
- Do not paste secrets, tokens or the contents of `~/.config/capsule/`.
- Component pages say how a part is built and where it runs. Keep step-by-step operating detail in the page of the part it belongs to.
