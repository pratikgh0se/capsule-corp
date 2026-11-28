-- irreversible: row 127 (additive nullable DDL, rollback is git revert; OBS-09 slice 3)
-- 006 factory envelope columns on events (docs/observability-conventions.md section 10).
-- Additive and nullable: every existing reader names its columns and is untouched.
-- Apply BEFORE the shipper that writes them (lookout_ship names these columns in its insert).
ALTER TABLE events ADD COLUMN IF NOT EXISTS event_name text;
ALTER TABLE events ADD COLUMN IF NOT EXISTS run_id text;
ALTER TABLE events ADD COLUMN IF NOT EXISTS trace_id text;
ALTER TABLE events ADD COLUMN IF NOT EXISTS span_id text;
CREATE INDEX IF NOT EXISTS events_trace ON events (trace_id) WHERE trace_id IS NOT NULL;
