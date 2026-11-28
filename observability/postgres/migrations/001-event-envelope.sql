-- irreversible: row 127 (C3/M062 - additive DDL, rollback is git revert)
-- 001 event envelope (docs/superpowers/specs/2026-09-07-board-seam-design.md 3.4).
-- Additive and nullable, so the previous events.py and its previous COPY_COLS
-- still match this table: rollback is `git revert`, with no down-migration.
ALTER TABLE events ADD COLUMN IF NOT EXISTS v smallint NOT NULL DEFAULT 1;
ALTER TABLE events ADD COLUMN IF NOT EXISTS event_id text;
-- Partial, so legacy rows (event_id IS NULL, v = 1 by the default above) and
-- new rows coexist. No synthetic backfill: a retroactive uuid carries no dedupe
-- information and would make the index claim a guarantee it cannot give.
CREATE UNIQUE INDEX IF NOT EXISTS events_event_id
  ON events (event_id) WHERE event_id IS NOT NULL;
