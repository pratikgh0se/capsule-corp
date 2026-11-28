# Retention - capsule-corp

One table, one row per store: what it holds, how long it is kept, why, what
enforces the keep, and whether that enforcement obeys the `OPS_RETENTION_APPLY`
dry-run flag.

| Store | Keep | Reason | Enforced by | Dry-run flag applies |
|---|---|---|---|---|
| Loki (logs) | 7 d | recent-activity debugging only | Loki itself (`retention_period: 168h`, compactor) | no |
| Postgres `events` | card 180 d, health 14 d, every other kind 30 d | the notes ledger is the long-term record; this copy is a recent-activity cache | `cleanup.sh` weekly `DELETE` (`maintenance/retention.sql`) | no (already live) |
| Postgres `samples` | raw 90 d; hourly rollup `samples_hourly` 730 d | raw rows feed the short-range panels; the 8-week weekly panels read the hourly rollup (D255) | `cleanup.sh` | yes |
| Postgres `cap_*`, `prices`, `cap_event_words` | forever (record tables, about 6 MB a year) | small, and history is the point | nothing (never deleted) | n/a |
| Worker session transcripts (`~/.claude/projects`, only session ids named in spool `run.json`) | compressed after 14 d once that week has a `fresh_tokens` eval_check row; archive deleted 30 d later | the tokens oracle needs the raw file until its week is recorded; the owner's own chats are never touched | `cleanup.sh` via `retention_transcripts.py` (zstd -19 --long, verified by sha256 round trip before the original goes) | yes |
| Unused Docker images | all unused images except compose images, the worker pin, the `FROM` base image of `docker/worker/Dockerfile` (parsed from the file; a rebuild would re-pull it, and fail offline) and container images | `image prune -a` cannot exclude, so `cleanup.sh` removes unused images one by one, never forced | `cleanup.sh` (`prune_images`); labelled Capsule images are handled by `maintenance/image_gc.py` and `maintenance/storage_gc.py` | yes |
| Test temp directories (top level of `TMPDIR`, our uid, names the suites create such as `capsule-suite-*`, `mutation-check-*`, `lander-box-*`) | deleted once older than 1 day with nothing inside modified within the day | suites that crash leak large temp trees | `cleanup.sh` | yes |
| Event spool (`events.jsonl`) | trimmed to what is not yet shipped to Postgres | everything before the shipped offset is already in Postgres | `cleanup.sh` (never trims when the offset is unreadable) | no |
| Lookout logs (`lookout/ship.log`, `lookout/publish.log`) | trimmed to the last 10,000 lines once over 5 MB | keeps the tail that explains a failure | `cleanup.sh` | no |
| Tick logs (`$CAPSULE_HOME/logs/tick*.log`, launchd stdout/stderr) | trimmed to the last 10,000 lines once over 5 MB | the only rotation they have; launchd holds them open | `cleanup.sh` (`trim_inplace`) | no |
| Maintenance job logs (the directory set by `CLEANUP_MAINT_LOGS`) | 7 d, 50 MB cap (last 10,000 lines kept; trimmed in place, same inode, since launchd holds the files open) | the maintainer's rule: nothing older than a week on the laptop | `cleanup.sh` (`del_old` / truncate on the job logs only) | no (already live) |
| Kanban boards | kept; never deleted; size sampled | archived cards are history | nothing (measured only) | n/a |
| Landing journal (`$CAPSULE_HOME/landing/<board>/<task>/journal.jsonl`) | kept; not pruned | a few small lines per landing (step, sha, card commits); the restart after a kill reads it | nothing | n/a |
| Backups | 7 tarballs (`BACKUP_KEEP`, counting the one written that night). The tarball also holds the Capsule boards (`boards/*/kanban.db` online backup, `config.json`, `handovers.jsonl`, `queue/*/owners.json`, `human/`, `intake/`); `restore.sh --verify` restores one into a temp home and runs `capsule check --board`. | a week of nightly restore points | `backup.sh` | no (already live) |
| Notes store | forever | it is the memory | n/a (never deleted) | n/a |
| Invoices and receipts (`~/Documents/Capsule/invoices`, `~/.local/share/capsule-corp/invoices`; neither exists by default) | **5 years minimum - statutory floor, not a choice** | Thai Revenue Code **s.87/3** (VAT registrant: at least 5 y from filing, the Director-General may require 7, plus 2 after cessation) and **s.105 bis** (non-VAT: receipt copies not less than 5 y) | `cleanup.sh` **refuses to delete** and says so every run (`is_statutory()`) | no - the refusal is unconditional |
| User data written into the notes store by the focus-timer app (`Tasks/Focus Log.md`, `Tasks/Todo.md`) | kept; **never deleted by any job** | the app appends one line per session (date, start, end, preset label and the todo text the user typed) and rewrites `Todo.md` in place. This is user-authored personal data, not Capsule telemetry | `cleanup.sh` refuses; the notes store is never deleted | no |

**The Lookout.** `events` kind `card` (all sources) 180 days; heartbeats, kinds
`health` and `tick_health` (all sources), 14 days, except each board's newest
`tick_health` row, which is kept however old (a tick dead past 14 days must keep
paging `capsule-tick-deadman`, which picks its boards from that latest row); every
other kind 30 days. The
rules live in `maintenance/retention.sql`, applied weekly by `cleanup.sh`.

## Where the data goes when it ages out

**Postgres `events`.** Nowhere, but the loss is by design: the notes ledger
already holds the milestones that matter (decision rows, card history, spec
approvals) written at the time they happened, so the Postgres copy is a
recent-activity cache for dashboards, not the system of record.

**Postgres `samples`.** Nowhere. Raw rows keep 90 days; `obs_probe_ship.py` also
upserts hourly avg/min/max/count into `samples_hourly` (migration 009), kept 730
days, which the weekly dashboard reads. Nothing downstream needs older rows.

**Loki (logs).** Nowhere. Loki's own compactor expires chunks past its 7-day
`retention_period`; Capsule does not read them back from anywhere else.

**Maintenance job logs.** Nowhere. `cleanup.sh` deletes files past 7 days and
truncates files over 50 MB to their last 10,000 lines; the dropped lines are
gone.

**Kanban boards.** Kept forever: this policy never deletes a row inside a
kanban database. The nightly `backup.sh` tarball holds point-in-time snapshots
of the board files, which is the closest thing to a history of what was pruned
or archived inside them.

## Statutory floors

Every row above except the last two is a retention choice, and a future
retention change may shorten any of them. The last two rows are different: their
keep is set by statute (invoices) or by the fact that the data is the user's own
(the focus-timer files), and **no retention change may shorten either**.
`cleanup.sh` enforces this with `is_statutory()`, which has no flag -
`OPS_RETENTION_APPLY` gates deletions, and these are not deletions that can be
enabled.

## The `OPS_RETENTION_APPLY` flag

`ops.env` carries `OPS_RETENTION_APPLY` (deploy writes `0` when the key is
missing; `CLEANUP_RETENTION_APPLY` overrides it for tests). Every deletion in
the "yes" rows of the table (`samples`, worker transcripts, unused Docker
images, test temp directories) runs behind that flag, checked independently of
the script's own `--dry-run` flag: a plain `--dry-run` run still shows these
sections as `would <action>` even if `OPS_RETENTION_APPLY=1`, and the flag
being `0` keeps them dry-run even on a real (non `--dry-run`) invocation.

- **`0` (default).** Nothing is deleted. Each candidate prints its own
  `would delete <path>` line, plus one line for the samples delete
  (`would delete samples older than 90 days` - rows are counted, not listed);
  the job then writes one `job` event line:
  `retention: dry-run, N files / M rows would go`.
- **`1`.** The deletes run for real, same code paths, same counts, without the
  `would` prefix.

`M` is read with a read-only `select count(*)` against Postgres, which a plain
`cleanup.sh --dry-run` must not do either (that mode invokes nothing at all,
docker included) - so in a full `--dry-run` the row count is genuinely unknown
and the line reads `N files / ? rows would go`. A real run with the flag at `0`
does read it, and prints the number.

The other sections (7-day job logs, events, spool and log trims, backups, the
dangling-image prune) run unconditionally and are not gated by this flag.

**Reading the pending count.** While the flag is `0`, `status.sh` shows a
`== retention` section reading `state/retention.json` directly:
`retention: dry-run (N files, M rows pending)`, with the file and row counts
kept separate. The daily digest instead prints one combined line after
`## Heal`, `retention: dry-run (N pending)`, where `N` is files plus rows (only
while the mode is `dry-run` - once `OPS_RETENTION_APPLY=1` is set the digest
line drops out entirely). The `job` event that `cleanup.sh` writes carries both
numbers separately too (`retention: dry-run, N files / M rows would go` or
`retention: applied, N files / M rows`), so the raw counts are always
recoverable from the event log.
