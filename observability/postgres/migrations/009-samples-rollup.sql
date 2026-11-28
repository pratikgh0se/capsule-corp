-- down: drop table if exists samples_hourly;
-- 009 samples rollup (D255: metrics stay on plain Postgres). samples already has the indexes the
-- time-range reads need (samples_ts, samples_name_ts, 002), so this adds no index. It adds
-- samples_hourly: one row per (service, name, hour) with avg/min/max/count, labels dropped, so the
-- 8-week weekly dashboard reads a small table and raw samples can keep 90 days (was 30).
-- maintenance/obs_probe_ship.py upserts the last 2 hours after every COPY; the backfill below
-- covers rows that already exist. Retention (cleanup.sh): raw 90 d, samples_hourly 730 d.
-- Rollback: run the down line above; dashboards and obs_probe_ship must be reverted first.
create table if not exists samples_hourly (
  bucket    timestamptz      not null,
  service   text             not null,
  name      text             not null,
  avg_value double precision not null,
  min_value double precision not null,
  max_value double precision not null,
  n         bigint           not null,
  total_avg double precision not null,
  ticks     bigint           not null,
  primary key (service, name, bucket)
);
-- Two stages: a tick writes one row per container, so the inner select sums a tick and the outer
-- select rolls the ticks up. avg_value is the mean per series row; total_avg is the mean per-tick
-- total (what the Containers panels show); ticks counts the ticks (n counts container rows).
insert into samples_hourly (bucket, service, name, avg_value, min_value, max_value, n, total_avg,
                            ticks)
select date_trunc('hour', ts), service, name, sum(s) / sum(c), min(mn), max(mx), sum(c), avg(s),
       count(*)
from (select ts, service, name, sum(value) s, count(*) c, min(value) mn, max(value) mx
      from samples group by 1, 2, 3) t
group by 1, 2, 3
on conflict (service, name, bucket) do update set
  avg_value = excluded.avg_value, min_value = excluded.min_value,
  max_value = excluded.max_value, n = excluded.n, total_avg = excluded.total_avg, ticks = excluded.ticks;
