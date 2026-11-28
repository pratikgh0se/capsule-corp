-- irreversible: row 127 (C3/M062 - additive DDL, rollback is git revert)
-- 002 samples (docs/superpowers/specs/2026-09-09-observability-build-1-design.md
-- section 3). Additive: a brand-new table, so there is nothing for an old
-- obs_probe.py (there is none yet) to break, and nothing to roll back beyond
-- `git revert` - no down-migration, same convention as 001.
CREATE TABLE IF NOT EXISTS samples (
  ts      timestamptz NOT NULL,
  service text        NOT NULL,   -- 'capsule' for everything in this build
  name    text        NOT NULL,   -- dotted, see docs/observability-conventions.md
  value   double precision NOT NULL,
  labels  jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS samples_name_ts ON samples (name, ts);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
