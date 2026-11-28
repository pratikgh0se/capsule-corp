-- irreversible: t_c671377a (additive indexes; rollback is dropping the two indexes)
-- 005 dead-man query indexes (card t_c671377a). Grafana dropped evaluation ticks for
-- backup-missing, backup-partial, disk-high and gateway-down; their ops-pg queries read the
-- latest row of a narrow slice of events. These two partial indexes make each of them a
-- one-row index lookup. Additive and idempotent; rollback is dropping the two indexes.
-- Host step: apply with `python3 maintenance/db_migrate.py` (index build briefly blocks writes).
CREATE INDEX IF NOT EXISTS events_health_ts ON events (ts DESC)
  WHERE kind = 'health' AND source <> 'capsule';
CREATE INDEX IF NOT EXISTS events_backup_job_ts ON events (ts DESC)
  WHERE kind = 'job' AND payload->>'job' = 'backup';
