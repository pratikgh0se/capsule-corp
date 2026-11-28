# Changelog

## Unreleased

## [v0.2.0-alpha.1] - 2026-11-28 (pre-alpha preview, first public release, GitHub pre-release)

Capsule Corp is a lightweight software factory for a solo maintainer: agents work cards on a board, and nothing lands on `main` until independent checks pass. This is an early preview, marked as a GitHub pre-release. It works on the maintainer's own machine and has not been tried on a fresh one. No app built by the factory has been accepted by its owner yet. A restructure into smaller, modular parts is planned, so the layout, commands and interfaces will change.

### New quality checks
- Plan trace check: every designed item maps to a card or a signed waiver.
- Owner preview hold: the factory waits for the owner's early look at the first slice.
- UI timing check: click-to-change and hang limits.
- UI motion check: designed motion must really move.

### Added
- Mac lane: a runner tier for cards that need macOS tools such as Xcode (`capsule/runners/mac.py`); off by default.
- The factory loop: cards on a per-board SQLite board, a tick that routes them and never calls a model, an implementer whose shell commands all run in a no-network Docker container, and host-run read-only reviewer, validator and guide roles. The lander re-runs the suites in a clean clone and pushes the exact commit it checked (dry run by default).
- Blind tests: a blind-tester role writes acceptance tests from a card's acceptance text without seeing the code.
- Senior review gate: independent reviewer and validator verdicts, plus mutation, size, simplicity, ruff and portability checks, before any landing.
- Incidents: every crash, hang or override opens an incident that closes only through a gate, and every human intervention is recorded (`capsule human`).
- One-command install on a Mac (`./install.sh`, with `--dry-run` to preview) that ends with `capsule doctor`, plus `capsule check` for read-only preconditions.
- Slack decisions: optional notifications and phone answers to blocked-card questions (off unless a board enables it).
- Observability stack: Grafana dashboards and alerts, a log shipper, and cost and weekly usage meters (opt-in meter).
- Local UIs: every local web UI is named `cc.<name>.localhost` behind a loopback-only Caddy front door.
- Secrets backend: `bin/capsule-secret` with `file` (default, mode 0600), a self-hosted secrets server or `env` backends; secrets never live in git.
- Public export: an allowlisted, linted export of the repository (`maintenance/public_export.py`, `maintenance/public_lint.py`) that never pushes by itself.
- Settings for personal details: owner name, copyright holder, config folder, notes vault and launchd label prefix (env `CAPSULE_LAUNCHD_PREFIX`, else `launchd.label_prefix` in the host settings, default `dev.capsule`) are settings, not hard-coded.
- MIT licence: first public release under the MIT licence. (t_5f7355f9)

### Changed
- Test runs pick the tests a change needs, with a full nightly run as the backstop.
- Agents can no longer record the owner as the author of a human action.
- The board bridge no longer needs an outside command-line tool, and the old tool's names are gone from the board schema and docs.
- Deploy is stricter: it refuses a scratch home when the real `launchctl` is in play, and refuses to create fresh passwords when a data folder cannot be read.
- Public CI runs on GitHub-hosted Mac runners, and the public release check is at zero findings.

### Security
- Every shell command of an implementer runs in a `--network=none` Docker container; the model session runs on the host, edits only its own clone, and the container holds no secrets. Reviewers, validator and guide use read-only tools.
- Secrets are pulled at start from the chosen backend, never committed; a gitleaks scan runs on every pull request, and the `file` backend refuses a file that is not mode 0600.
- The local front door listens on 127.0.0.1 only.
- Release files ship with `SHA256SUMS`; build provenance is attached once the repository is public.
- Report vulnerabilities privately; see `SECURITY.md`.

### Known limitations
- Open problems, with workarounds, are listed in [docs/KNOWN-ISSUES.md](docs/KNOWN-ISSUES.md).
- No finished sample app ships with this release.
- macOS-first: the install scripts refuse to run elsewhere, and Linux is untested.
- Claude Code CLI and login required: it is the only implemented runner so far.
- Not yet verified on a fresh machine: the install path has been exercised on the author's machines and in tests only.
- The worker image is built by you, once (about 40 GB of disk, hours of build time); there is no hosted download.
- The macOS sandbox profile for read-only roles is tested but not yet wired into live runs.
- The implementer's AI session can read any file your macOS user can read; its edits are limited to its clone. Run Capsule under a separate macOS user account with no personal files or secrets.
- Single machine, single maintainer: no multi-user mode and no hosted version.
- Some defaults, such as example board configs, are still the author's own; review them before installing.

## Earlier internal development (not public)

Pre-release history before the first public version; see the v0.1.0 entry below.

## v0.1.0 - 2026-09-11

- Power Level (the meter): cards + power collectors, ledger backfill, window pre-flight, report lines, Budget row, RUNBOOK (28c3d07)
- C2 budgets and stop conditions (c466247)
- C3 guards before capability (3298a7c)
- C4 memory and retrieval (dd6a63e)
- C5 correctness over green (20214f1)
- Secrets cutover: pull-at-start secrets via capsule-secret, 5-minute heal job, chat outbox, cutover script (931ddc9)
- Cutover: --scan-only runs the step 7 leftover scan alone after a completed cutover (fd73f62)
- Merge gate ledger backfill applied for 28 cards (t_1e4abc93)
