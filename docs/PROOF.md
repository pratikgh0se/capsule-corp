# What has been proved, and how

This page lists what Capsule Corp has proved, what it has proved only in part, and what it has not proved. Every claim has a number, a date and a source that you can check in this repository. Counts were taken on 2026-11-28 from public commit `7158206bc` plus this page. The project is a pre-alpha preview (`v0.2.0-alpha.1`). Read [KNOWN-ISSUES.md](KNOWN-ISSUES.md) for open problems.

## Proved

| Claim | Number, as of 2026-11-28 | How to check |
|---|---|---|
| The harness core has a unit test suite. | 6,417 tests collected in `capsule/tests` (422 test files). The `Verify` run on the public repository (run 46094865224) passed them all on 2026-11-28 in about 10 minutes (public commit `7158206bc`). | `python3 -m unittest discover -s capsule/tests -t .`; CI step in `.github/workflows/verify.yml`; the `Verify` runs on the Actions tab of the public repository. |
| The maintenance scripts have a unit test suite. | 2,937 tests collected in `maintenance/tests`. The same `Verify` run passed them in about 80 seconds. | `python3 -m unittest discover maintenance/tests`; the `Verify` runs on the Actions tab of the public repository. |
| The public lint finds no personal paths or names in the files that ship. | 1,595 files scanned, 0 hits, every category at its baseline of 0. | `python3 maintenance/public_lint.py --baseline release/public-lint-baseline.txt` (maintainer tree only: it needs release files that do not ship). |
| Shipped docs link only to shipped files. | Two tests. One checks every link in every shipped `.md` file. | `python3 -m unittest maintenance.tests.test_public_doc_links`. |
| CI runs on every pull request and every push to `main`. | The `Verify` workflow runs on every pull request, every push to `main` and nightly. Two more workflows exist: `Worker images` (pushes that change `docker/`, and weekly) and `Release checksums` (version tags). | `.github/workflows/verify.yml`, `image.yml`, `release.yml`; `bash tests/test_ci_yaml.sh` passed on 2026-11-28. |
| CI builds the worker image and probes it on Linux. | The `Verify` job builds the `base` and `python` layers, then runs `tests/test_docker_image.sh`. It also creates a Docker context named `orbstack` so Capsule's pinned engine name resolves on the runner. | Steps "build the worker image" and "docker context for the default engine" in `.github/workflows/verify.yml`. |
| A secret scan runs in CI. | gitleaks 8.30.1, pinned and checksummed, on every pull request and nightly. | Job `security-scan` in `.github/workflows/verify.yml`; config `.gitleaks.toml`. |
| A new behaviour needs a test that fails without it. | The mutation check and the size check run against the commit a change branched from. | `tests/mutation_check.py`, `tests/size_check.py`; [CONTRIBUTING.md](../CONTRIBUTING.md). |
| The reliability rules are written down and checked against drift. | One rules file. A test fails when the public mirror differs from its recorded checksum. | [ZENKAI.md](ZENKAI.md); `capsule/tests/test_zenkai_mirror.py`. |
| Every crash, hang or override opens an incident, and an incident closes only through a gate. | Implemented in code, with tests. | `capsule/incidents.py`; [architecture/components/incidents-korin-flow.md](architecture/components/incidents-korin-flow.md). |
| Blind tests exist as a mechanism. | A blind-tester role, a gate and a red check. 32 `test_blind_*.py` files in `capsule/tests`. | `ls capsule/tests/test_blind_*.py`; decisions in [DECISIONS.md](DECISIONS.md). |

## Partly proved

| Claim | What is proved | What is missing |
|---|---|---|
| The four new proof-of-work gates work: plan trace, owner look, UI timing and UI motion. | The code exists: `capsule/plan_trace.py`, `capsule/preview.py`, `capsule/ui_timing.py`, `capsule/ui_motion.py`. [CHANGELOG.md](../CHANGELOG.md) lists them. | No test file in `capsule/tests`, `maintenance/tests` or `tests` names them (`grep -rl "ui_timing\|ui_motion\|plan_trace" capsule/tests maintenance/tests tests` returns nothing). No finished app has passed them. Treat them as unproven. |
| The factory lands work on its own. | Auto-landing is on for the `capsule-corp` board (`lander.mode: land`). The README says so. | No public record of how many cards landed. Check `capsule status` on your own board. |
| The install works on a Mac. | The dry run runs in CI on a macOS runner ([SETUP.md](SETUP.md), "Verified so far"). The install has run on the maintainer's machines. | Nobody has run a full install on a stranger's fresh Mac. See [KNOWN-ISSUES.md](KNOWN-ISSUES.md), item 1. |
| Swapping the model behind a role is safe. | The validator has a golden set (`capsule/tests/fixtures/golden/validator/`, 2 cases at 2026-11-28). `capsule golden run` replays it. | No other role has a golden set. |
| A passing test suite means good work. | This claim is false, and the project says so. The maintainer reports that a timer app built by the factory passed 1,301 tests and 64-frame golden comparisons, and judged it "extremely bad". | The 1,301 count is the maintainer's account. No public file records it. The gates above exist because of this failure. |

## Not proved yet

- That the factory can build an app that its owner accepts. No app built by the factory has been accepted yet ([README.md](../README.md), "Read this first").
- That Capsule runs on Linux. The harness is plain Python and its tests run on Linux CI. The scheduler and installer have not run there ([SETUP.md](SETUP.md)).
- That Capsule works with a model other than Claude. Only the Claude Code runner and a test stub exist (`capsule/models/claude.py`).
- That the read-only sandbox protects the reviewers. The tier is built and off by default (`redesign.light_runner`, `capsule/config.example.json`).
- That the system scales beyond one maintainer and one machine. It is built for one.

See [KNOWN-ISSUES.md](KNOWN-ISSUES.md) for the full list of open problems.

## How to repeat these checks

```bash
export HOME="$(mktemp -d)" AGENT_OPS_NO_LAUNCHCTL=1
python3 -m unittest discover maintenance/tests   # about 80 s in CI
python3 -m unittest maintenance.tests.test_public_doc_links
bash tests/test_ci_yaml.sh
```
