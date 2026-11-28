# Architecture overview

A one-page map. This page says what the parts are and where to read more. The full set is in [architecture/](architecture/README.md).

## The loop

1. **Intake.** A human or the chief-of-staff (CoS) agent files a card: `capsule intake <board> ...` (`capsule/intake.py`). A card is a row in the board's SQLite file under `$CAPSULE_HOME/boards/<board>/`.
2. **Tick.** One tick per board (`python3 -m capsule tick`, run by `bin/capsule-tick.sh` under launchd) is the only writer of that board. Each pass it routes cards to roles, enforces caps (running, runtime, failures) and spawns runs. It never calls a model. State changes go through the single transition table in `capsule/db.py`.
3. **Runs.** The tick starts one Docker container for the implementer (`docker/base` plus a stack, `--network=none`; `capsule/runners/docker.py`). `capsule/worker.py` then runs the model (`claude -p`) as a host process in the implementer's own git clone. The model edits files in that clone with host file tools. Every shell command it runs goes into the container through the `mcp__capsule__exec` tool (`capsule/runners/exec_mcp.py`), so commands have no network. Only the implementer gets a container. Roles: implementer, reviewer, validator, guide, Korin, security reviewer, lookout, advisor (proposals from rework, incident and override data) and auditor (board audit); both are run by hand, no scheduled job runs them yet. Each works under a contract in `contracts/`. The model for each role is chosen per board in `config.json` (`routing`); the only implemented runner is Claude Code (`capsule/models/claude.py`).
4. **Review.** After the implementer, the tick starts the reviewer, validator and guide in parallel (`_review_one` in `capsule/tick_review.py` starts every missing review role). They run on the host with read-only tools (`HostRunner`), not in containers. The reviewer and validator check every change. Hidden tests and a simplicity check (Z8) are part of the gate. The reviewer also audits each numbered acceptance row of the card (`capsule/contract_audit.py`): `contract_audit` in its verdict, active once the schema carries the field; a soft check that adds a `notes` event and never refuses the verdict.
5. **Keeper and landing.** The lander re-runs the suites in a throwaway clone and, in `land` mode, pushes exactly the commit it checked to `origin/main`. With no `lander.mode` the lander is off; `dry-run` checks only. See [lander-and-keeper](architecture/components/lander-and-keeper.md).
6. **Humans and incidents.** A blocked card, crash, hang or override is recorded (`capsule human`, `capsule/incidents.py`). Korin writes the post-mortem, and an incident closes only through the gate (the one exception: pre-Korin incidents opened before the row-638 analysis cutoff, whose issue is no longer open, that the CoS lists with evidence and a list hash, `capsule/incident_history.py`, row 638). Notifications go to a file outbox and, if enabled, Slack.

## Detailed architecture set

The architecture doc set (kept so anyone can understand how Capsule works, D99) is in [docs/architecture/](architecture/README.md): the [system overview](architecture/overview.md), one page per component (see the [components folder README](architecture/README.md#component-index)), a file-to-page [inventory](architecture/inventory.md) and the [planned designs](architecture/planned.md). This page stays the one-page map; a behaviour change updates the component page in the same change.

## Supporting parts

| Part | Where | Read |
|---|---|---|
| Harness CLI | `capsule/` (`capsule --help`) | [tick-scheduler](architecture/components/tick-scheduler.md) |
| Role contracts | `contracts/` | each file |
| Worker image | `docker/base/`, `docker/stacks/`, record `docker/IMAGES.lock` | [worker-runner-and-images](architecture/components/worker-runner-and-images.md) |
| Operator scripts | `bin/` (`capsule-tick.sh`, `capsule-secret`, `hoi-poi.sh`, `capsule-park.sh`) | [deploy-and-drift](architecture/components/deploy-and-drift.md) |
| Scheduled jobs | `launchd/templates/`, `maintenance/` (the AI jobs start `claude -p --restricted --strict-mcp-config --permission-mode dontAsk --tools ...` with the write guard always attached; empty `GUARD_ALLOW` writes nothing) | [maintenance-jobs](architecture/components/maintenance-jobs.md) |
| Install and drift | `./install.sh`, `capsule doctor` (the public path; deploy.sh is maintainer only) | [SETUP.md](SETUP.md) |
| Observability | `observability/` (Postgres, Loki, Fluent Bit, Grafana, Caddy, Infisical) | [observability](architecture/components/observability.md), `docs/observability-conventions.md` |
| Secrets | Infisical, `bin/capsule-secret` | [secrets](architecture/components/secrets.md), `docs/EGRESS.md` |
| Backups and retention | `maintenance/` jobs | [maintenance-jobs](architecture/components/maintenance-jobs.md), `docs/RETENTION.md` |
| CI | `.github/workflows/` (tests, gitleaks, image build) | [worker-runner-and-images](architecture/components/worker-runner-and-images.md) |

## Design rules

- **Reliability first.** The Zenkai rules in [ZENKAI.md](ZENKAI.md) bind every agent (precedence, overrides, autonomy by reversibility, simplicity). Checks need two independent oracles.
- **Thin around the model.** Machinery stays small so a stronger or weaker model does not break it. See [PRODUCT.md](PRODUCT.md) for the north star and scope rules.
- **One pane per kind of thing.** Needs-you is a message, inspectable state is a dashboard or `capsule status`, records are the vault.
- **No-network implementer commands.** On the strong tier (the default), every shell command of the implementer runs in a `--network=none` container with no secrets, and dependencies come from a vendored read-only cache. The light tier (`redesign.light_runner`, off by default) runs the commands on the host under a seatbelt profile (`capsule/runners/seatbelt.py`). The model session itself runs on the host and edits only its own clone.

## History

The factory now runs on the Capsule tick; an older agent runtime and its setup steps are removed (D198/D202), and the maintainer's older operating notes still carry that era's decision trail. The current commands are in [tick-scheduler](architecture/components/tick-scheduler.md). Milestones are in [CHANGELOG.md](../CHANGELOG.md).
