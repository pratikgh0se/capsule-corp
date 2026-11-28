-- Retention for ops-pg `events` (Lookout spec 5.11, D5, row 478 b). Run weekly by cleanup.sh.
-- All `card` events share one horizon, whatever their source: an open and its close must age out
-- together, or a per-source horizon would resurrect a closed "needs me" row.
-- cap_*, prices, cap_event_words are never deleted. samples stay behind OPS_RETENTION_APPLY.
delete from events where kind = 'card' and ts < now() - interval '180 days';
-- Each board's newest tick_health row is never deleted, however old: capsule-tick-deadman picks the boards
-- to watch from each board's latest heartbeat, so a tick dead past 14 days must keep paging, not drop out.
with keep as materialized (select board, max(ts) as last from events where kind = 'tick_health' group by board) delete from events e where e.kind in ('health', 'tick_health') and e.ts < now() - interval '14 days' and (e.kind = 'health' or e.ts < (select k.last from keep k where k.board is not distinct from e.board));
delete from events where kind not in ('card', 'health', 'tick_health') and ts < now() - interval '30 days';
