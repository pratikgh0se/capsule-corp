-- down: drop table if exists live_containers, live_health, live_seats;
-- 007 live snapshot (W347): machine state maintenance/live_snapshot.py replaces every minute for the
-- System Now dashboard. Whole-table replace in one transaction, so there is no history to keep.
create table if not exists live_containers (
  name text primary key, kind text not null check (kind in ('service', 'worker')),
  image text, status text, up_for text, created_at timestamptz,
  board text, run_id text, snap_ts timestamptz not null);
create table if not exists live_health (
  check_name text primary key, label text not null, ok boolean, value double precision,
  detail text, snap_ts timestamptz not null);
create table if not exists live_seats (
  seat text primary key, used int not null, cap int, holder text, snap_ts timestamptz not null);
