# Maintenance jobs (launchd and `maintenance/`)

## Purpose
Scheduled custodial work around the tick: self-healing, backups, cleanup, probes, quality, and the
author's vault jobs. None of these is in the card loop; they keep the machine honest.

## Where it lives (files)
- Schedules: `launchd/templates/*.plist` (calendar or interval jobs), rendered and registered by deploy.sh (maintainer only, not in the public edition). Job classification (DISPATCHING, CUSTODIAL) is in `bin/lib_jobs.sh`.
- Runner: `maintenance/run.sh <job>` (stamps `state/jobs/<job>.json` for every run; skips before any model call when idle; pure Python or bash jobs never call a model). Libraries: `lib_compose.sh`, `lib_send.sh`, `lib_state.sh`.
- Job scripts: `heal.sh` (+ `heal_table.py`), `backup.sh` + `backup_key.sh`, `restore.sh`, `cleanup.sh`, `deps_sync.sh` + `deps.py`, `quality_nightly.py`, `sbom.sh`, `obs_probe.py`, `obs_inventory.py`, `live_snapshot.py`, `tailnet_serve.sh`, `grafana_prune_folders.sh`, `retrieval_eval_job.py`, `usage_guard.py`, `report.sh`, `status.sh`, `disk_relief.py`, `storage_gc.py`, `blind_mutation.py`, `image_gc.py`, `ops_preview.py` (see [cos-tools](cos-tools.md)); `run.sh` dispatches these by job name.
- Shared modules: `events.py` (the one event spool every script writes; `state_root()`), `budgets.py`, `messages.py`, `scrub.py`, `pricing.py` + `prices.json`, `decision_log.py`, `ledger.py`, `absence.py`, `verdicts.py`, `vault_git.py`, `vault_jobs.py`, `vault_stats.py`, `ai_usage.py`.
- Earlier-prototype leftovers (some are still imported: `budgets.py` by `capsule/intake.py`, `decision_log.py` by `capsule/touches.py` and `capsule/vault_context.py`, `vault_git.py` by `capsule/tick.py`, `board_bridge.py` by `capsule/brainstorm.py`): `board.py`, `board_bridge.py`, `board_view.py`, `board_fence.py`, `board_errors.py`, `merge_gate.py` (helpers only since D238: spec and release stamps, `run_in_clone`, `test_cmd_for`, state dir), `intake.py`, `spec_lint.py`, `docs_sync.py`, `decisions.py`, `epic_lane.py`, `contracts.py`, `ledger.py`, `dark_test.py` (a measured 48-hour dark test), `transcripts.py` (golden transcripts for role SOULs), `hooks/` (PreToolUse write guards for unattended vault jobs).
- Release and legal gates (checks run by hand or by the author's tooling, not by the tick):
  `release_gate.py`, `policy_check.py`, `product_privacy.py`, `notice_gen.py` (also `--deep-review
  <trivy json>...` for the weekly licence deep scan: exit 0 clean, 1 new findings listed in the CI
  run summary, 2 report missing or unreadable; accepted findings are tab-separated `licence<TAB>path
  glob<TAB>reason` lines in `docs/licenses/deep-scan-accepted.txt` (never for GPL/AGPL in a shipped path; SPDX `OR`/`AND` expressions are evaluated, unparseable ones denied); `.github/workflows/image.yml`
  runs two trivy licence steps, one per worker image, then this review, report-only),
  `public_lint.py`, `migration_lint.py`, `order_book_lint.py`. Full file-to-page map:
  [../inventory.md](../inventory.md).

## Inputs and outputs
In: the clock; board DBs and the vault (read-only for most). Out: `infra` and `job` events into the spool, files in the state root, outbox messages, backup tarballs, Postgres rows.

## State it reads and writes
State root `~/.local/state/capsule-corp/maint` (`AGENT_OPS_STATE_DIR`); `~/Backups`; the vault (author overlay jobs only).

## Key rules and invariants
- Capsule jobs (prefix from `bin/lib_prefix.sh`): `capsule-tick-<board>` (KeepAlive; one template per board), `disk-relief` (every 4 h, D204), `blind-mutation` (Sunday 05:30, report only, install-only), `heal` (every 300 s, at load), `patrol` (every 1800 s, at load, install-only; [patrol](patrol.md)), `keeper-watch` (every 900 s, install-only), `live-snapshot` (every 60 s), `obs-probe` (every 600 s), `backup` (03:35), `deps-sync` (03:40), `quality-nightly` (04:10), `quality-full` (01:00, D273 full suites of capsule-corp), `restore-verify` (Sunday 04:05), `cleanup` (Sunday 02:03), `fork-sync` (Sunday 02:20), `obs-inventory` (Sunday 02:40), `docker-idle` (every 300 s, install-only), `brainstorm` (every 60 s, install-only), `slack-listen` (at load, KeepAlive, install-only; [slack-listen-job](slack-listen-job.md)).
- Author vault overlay: the maintainer also runs a set of personal note-keeping jobs (planning, nightly and weekly notes, reports, a local search server). They are not part of the public edition and their templates do not ship.
- `heal` is pure bash, no model: it runs checks in order (`docker`, `infisical`, `stack`, `secrets`, `slack-token` (report only, never pages), `claude-login`, the tick watchdog `tick`, `patrol`, `jobs`, `outbox` flush), writes one `infra` event per check, keeps counters in its state file under the state root, and alerts only on a second consecutive docker failure and on Claude logout (12 h throttle). The `tick` check runs `capsule tick-watch --apply --json` (`capsule/tick_watch.py`): it records `tick ok`, `tick fail` (a board is stopped, dead, wedged or in a crash loop) or `tick resumed` (a board is beating again). The `patrol` check reads `patrol.beat`: `patrol fail` when it is older than 75 min (one `launchctl kickstart`, then a page on the next run), `patrol resumed` when it is fresh again, `patrol skipped` when no beat file exists. The `jobs` check runs `capsule tick-watch --jobs` over the pipeline labels and bootstraps a lost job (no stop marker, not parked); `tick-watch --ping` sends the optional dead-man GET (`unattended.deadman_url`). Its compose calls go through the [compose live guard](compose-live-guard.md).
- `backup` also runs `PRAGMA wal_checkpoint(TRUNCATE)` on each board db once a week (stamp `wal-checkpoint.stamp` in the state root); no row is deleted. It writes a nightly tarball of everything not on GitHub (encrypted only when `OPS_BACKUP_ENCRYPT=1`; the default is off), keeps the newest `BACKUP_KEEP` (default 7); `restore-verify` unpacks one into a throwaway directory weekly. `cleanup` removes anything older than a week (`--dry-run` available; deploy.sh, `cleanup.sh` apply are operator actions).
- `quality_nightly.py` writes the last 40 lines of a red full-phase suite to the job log, redacted by `scrub.py` and indented under the verdict line, so a red night can be diagnosed from the log alone. The finding title never carries them.
- A job failure is recorded on its `job` event; the Grafana job-failed alert owns the page.
- Jobs on a quiet or parked pipeline: `park` leaves CUSTODIAL jobs running ([deploy-and-drift](deploy-and-drift.md)).

## Failure modes and what happens
Docker down: heal records skipped checks and throttled alerts. Backup failure: job event red, alert. A job with nothing to do records a skip, not a failure.

## Tests that pin it
Shell: `tests/test_run_sh.sh`, `test_heal.sh`, `test_backup.sh`, `test_backup_capsule.sh`, `test_restore.sh`, `test_cleanup.sh`, `test_deps_sync.sh`, `test_status.sh`, `test_sbom.sh`, `test_tailnet_serve.sh`, `test_state_root_shell.sh`, `test_lib_compose.sh`. Python: `maintenance/tests/` (`test_events.py`, `test_obs_probe.py`, `test_live_snapshot.py`, `test_quality_nightly.py`, `test_usage_guard.py`, ...). Intent: `contracts/intent/heal.jsonl`, `live_snapshot.jsonl`, `obs_probe.jsonl`, `meters.jsonl`, `usage_guard.jsonl`, `lib_jobs.jsonl`.

## Related decisions and specs
docs/superpowers/specs/2026-09-07-backups-design.md (maintainer only, not in the public edition), `2026-09-08-secrets-cutover-design.md` (heal), `2026-09-10-continuity-and-data-safety-design.md`, `2026-09-13-dep-cache-design.md`; this page.

- `maintenance/docker_idle.py` (D195, D204): quits Docker Desktop once it has been idle for a while; launchd runs it with `--apply` every 5 minutes, no flag only prints.

## Status
current for the capsule jobs. The capsule jobs' templates are `__PREFIX__.capsule-corp-*.plist`; the prefix is the `launchd.label_prefix` setting. launchd/unloaded (maintainer only, not in the public edition) lists the install-only labels: the keeper-watch, blind-mutation, docker-idle, slack-listen, brainstorm and patrol jobs.
