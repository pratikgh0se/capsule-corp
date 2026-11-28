# Contributing

Thanks for looking. Capsule Corp is a pre-alpha, single-maintainer project under the MIT licence
(see [LICENSE](LICENSE)). This page says what you can do today, and how to read, run and test the code.

Everyone who takes part must follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## What you can do now

Pull requests are closed today. Code changes come from the maintainer, and a pull request from
outside will be closed without review. This may open up later. What you can do now:

- **Open an issue.** Bug reports and ideas are welcome on GitHub. First read
  [docs/KNOWN-ISSUES.md](docs/KNOWN-ISSUES.md) to see if the problem is already listed.
- **A good bug report has:**
  - what you did, what you expected and what you saw instead;
  - the steps to reproduce it, numbered;
  - your macOS version, the Capsule Corp commit (`git rev-parse --short HEAD`) and your Docker
    engine and version;
  - the output of `capsule doctor`.

  The bug template in `.github/ISSUE_TEMPLATE/bug.md` asks for all of this.
- **A good idea issue has:** the problem you have (not only the feature you want), who is hurt
  by it, what you do today as a workaround, and how it fits the goals in
  [docs/PRODUCT.md](docs/PRODUCT.md) (lightweight, reliable first, thin around the model).
- **Read and run the code.** The sections below show how to set it up and run the tests.

## Security reports

Do not open a public issue or pull request for a vulnerability. Use GitHub private vulnerability
reporting, as described in [SECURITY.md](SECURITY.md).

## Read first

- [docs/PRODUCT.md](docs/PRODUCT.md): the north star and product boundary. A change should serve
  it: lightweight, reliable first, fewest lines that do the job, commodity features left to the
  tools that already ship them.
- [docs/ZENKAI.md](docs/ZENKAI.md): the reliability rules. Cite them by item, do not copy them.
  The ones that matter most for a contributor:
  - Z1: a script, check or contract ships with example inputs and the outcome each must produce.
  - Z2: never rely on one oracle; a change is checked by two independent means.
  - Z7 and Z8: less code; the simplest design that does the job.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): a one-page map of the parts.

## Code map

Paths are from the repository root. The last column links to the component page that explains the
part in detail ([all pages](docs/architecture/README.md)).

| Path | What it does | Page |
|---|---|---|
| `capsule/tick.py` | The tick: one pass of the factory loop (class `Tick`). Reads the board, starts and closes runs, rules on reviews. | [tick-scheduler](docs/architecture/components/tick-scheduler.md) |
| `capsule/db.py` | The SQLite board: tables, the card state machine, events. | [board-store](docs/architecture/components/board-store.md) |
| `capsule/dispatch.py` | Decides which card may start next: seats, caps, holds (entry point `dispatch.ready`). | [dispatch-and-caps](docs/architecture/components/dispatch-and-caps.md) |
| `capsule/worker.py` | Runs one model worker for one card (a host process; the tick starts the Docker container its shell commands use) and writes its result. | [worker-runner-and-images](docs/architecture/components/worker-runner-and-images.md) |
| `capsule/lander.py` | Checks a finished, approved card in a throwaway clone (gates, suites, deploy check). `capsule/landing.py` does the push to `main`. | [lander-and-keeper](docs/architecture/components/lander-and-keeper.md) |
| `capsule/intake.py` | How new cards and cross-board files enter a board. | [intake](docs/architecture/components/intake.md) |
| `capsule/human.py` | The human verbs (answer, approve, and so on) that people use on the board. | [intake](docs/architecture/components/intake.md) |
| `capsule/incidents.py` | Opens, tracks and closes incidents; files the fix card. | [incidents-korin-flow](docs/architecture/components/incidents-korin-flow.md) |
| `capsule/runners/` | Where a worker runs: Docker, host, macOS sandbox. | [worker-runner-and-images](docs/architecture/components/worker-runner-and-images.md) |
| `capsule/blind/` | The blind-test path: tests written without seeing the code. | [blind-path](docs/architecture/components/blind-path.md) |
| `capsule/prompts/` | Only `brainstorm.md` (the brainstorm prompt). Role prompts are the `contracts/*.md` files, assembled by `capsule/bundles.py`. | [brainstorm-runner](docs/architecture/components/brainstorm-runner.md) |
| `bin/` | Shell and Python entry points: tick wrapper, keeper watch, secrets, compose helpers. | [inventory](docs/architecture/inventory.md) |
| `maintenance/` | Scheduled upkeep jobs and the public lint. | [maintenance-jobs](docs/architecture/components/maintenance-jobs.md) |
| `contracts/` | The role contracts and the intent contracts that gates check. | [gate-intent-contracts](docs/architecture/components/gate-intent-contracts.md) |
| `docker/` | The worker base image and its pinned image list. | [worker-runner-and-images](docs/architecture/components/worker-runner-and-images.md) |
| `launchd/` | Templates for the macOS scheduled jobs. | [tick-board-plist](docs/architecture/components/tick-board-plist.md) |
| `observability/` | Logs and dashboards stack: Fluent Bit, Loki, Grafana, Postgres, Caddy. | [observability](docs/architecture/components/observability.md) |
| `capsule/tests/` | Python unit tests for the `capsule` package. | none |
| `tests/` | Shell suites and the mutation and size checks. | none |

Every module has a row in [docs/architecture/inventory.md](docs/architecture/inventory.md).

## Set up

This section and the next two are for reading and running the code locally, and for when pull
requests open.

Needs macOS 14 or newer, Python 3.11 or newer and git 2.40 or newer. The tests use the standard
library only. The size check below also needs `pip install ruff==0.17.4`.

```bash
git clone <your fork or this repo>
cd capsule-corp
./install.sh --dry-run     # lists every setup step; changes nothing
```

The full install (Docker, Homebrew packages, launchd jobs) is only needed to run the factory
itself, not to work on the code. See [docs/SETUP.md](docs/SETUP.md).

## Run the tests

These are the unit suites that CI runs (`.github/workflows/verify.yml`). They need no Docker and no
network. Set a throwaway `HOME` so a test never touches your real one.

```bash
export HOME="$(mktemp -d)" AGENT_OPS_NO_LAUNCHCTL=1
python3 -m unittest discover -s capsule/tests -t .     # whole package
python3 -m unittest discover maintenance/tests         # maintenance scripts
bash tests/test_ci_yaml.sh                             # one shell suite
```

Size and speed, from a run on 2026-11-27 on a Mac. The full `capsule/tests` run is large: about
6,300 tests in 422 files, and it takes hours on a laptop, so CI runs it. Locally, run one file or
one test while you work:

```bash
python3 -m unittest capsule.tests.test_blind_gate_cap    # 19 tests, OK, 26 s
python3 -m unittest maintenance.tests.test_scrub         # 42 tests, OK (1 skipped), 1 s
bash tests/test_ci_yaml.sh                               # all passed, 4 s
```

`maintenance/tests` holds 2,937 tests (counted on 2026-11-28). CI runs them in about 80 seconds.

Shell suites live in `tests/` and run one at a time. CI runs a longer list of them; the
`RUN_DOCKER_TESTS=1` ones (`test_docker_image.sh`, `test_migration_pg.sh`, `test_alert_rules_pg.sh`,
`test_fluent_bit_scrub.sh`) need Docker and are not run unless you set that variable.
CI builds the `base` and `python` worker image layers first, because `test_docker_image.sh` probes
`capsule-python:local`, and it creates a Docker context named `orbstack` on the Linux runner so that
Capsule's pinned engine name resolves (`.github/workflows/verify.yml`). Some shell suites skip
checks on files that exist only in the maintainer's private tree, such as private prompts, owner
launchd plists and the maintainer's repo registry. In the public copy those checks skip; they do not fail. CI also runs
`ruff check --select E9,F63,F7,F82 .` (needs `pip install ruff==0.17.4`). Tests never use the
network, real repositories or real chat tokens.

Every new behaviour needs a test that fails without it (Z1, Z2). A change also has to pass these
checks, each against the commit it branched from:

```bash
python3 tests/mutation_check.py --base <base-commit> --head HEAD
python3 tests/size_check.py --base <base-commit> --head HEAD
```

The mutation check confirms your tests fail when the new code is broken. The size check refuses
growth past the recorded baseline; do not raise a baseline to make it pass.

## Public lint

Maintainer only: the public lint (`maintenance/public_lint.py`) needs release files that do not ship.

## Commit style

- Small, plain commits: one logical change each, with a short imperative subject.
- Say what changed and why, in plain words. No secrets, personal data or machine paths.
- Do not edit [docs/ZENKAI.md](docs/ZENKAI.md); it is a protected mirror.

## Docs

Docs that ship may only link to files that ship. The release allowlist decides what ships; a
path missing from it is private. If you add a doc and link it, ask for it to be added there too.

## Where to start

1. Read [docs/architecture/overview.md](docs/architecture/overview.md): the loop, the parts and
   the data stores on one page.
2. Follow one pass of the tick: `capsule/tick.py`, method `Tick.once`, then `_step_pass`,
   `_close_runs` and `_verdict`.
3. See how a card is chosen to start: `capsule/dispatch.py`, functions `ready`, `running_count`,
   `admit_reviews` and `hold`.
4. See how a finished card reaches `main`: `capsule/lander.py`, function `run`, then `capsule/landing.py`
   ([lander-and-keeper](docs/architecture/components/lander-and-keeper.md)).
5. See how a failure becomes an incident: `capsule/incidents.py`, functions `open_incident` and
   `file_fix_card`.

6. Learn the words and the data: [glossary](docs/architecture/glossary.md) and
   [data model](docs/architecture/data-model.md).
7. Follow one card from start to landing: [walkthrough](docs/architecture/walkthrough.md). To add
   a role, a gate or a check, read [extending](docs/architecture/extending.md).

## Docs rule

A change in behaviour updates its component page under `docs/architecture/components/` in the same
change. This is the repo rule; see [docs/architecture/README.md](docs/architecture/README.md) for the
page template and the index. If no page covers your change, add one and list it there.
