#!/usr/bin/env python3
"""The three Capsule Corp dashboards, as code.

regenerate: python3 observability/grafana/build_dashboards.py

The JSON in dashboards/ is generated - edit this file, re-run it, and commit both.
Running it must leave `git status` clean when nothing here has changed. Stdlib only.
"""
import json
import os
from pathlib import Path

from dash_lib import (  # noqa: F401
    PG, LOKI, SQ, LEVEL1, DRILL, pgt, sqt, lokit, thr, GREEN_ONLY, TEXT_ONLY,
    OUTCOME_COLORS, IDLE_MAP, OUTCOME_MAP, ROLE_MAP, SEVERITY_MAP, STATE_MAP, gp, base_fc,
    text_panel, stat, gauge, bargauge, TS_CUSTOM, timeseries, barchart, timeline, table, logs,
    row, dashboard)
from dash_sql import (  # noqa: F401
    ROLES, JOBS, VAULT_JOBS, USD_NOTE, role_pivot, NEEDS_ME_COUNT_SQL,
    AI_LATEST_ROWS, AI_LATEST_WHERE,
    TOKENS_24H_SQL, TOKENS_24H_DESC, TODAY_EXCLUDED, ai_daily, data_link,
    severity_override, url_cell_override)

OUT = Path(__file__).resolve().parent / "dashboards"
PLUMBING = OUT / "plumbing"


def histogram(pid, title, desc, ds, targets, grid, unit="h"):
    custom = {"lineWidth": 1, "fillOpacity": 80, "gradientMode": "none",
              "hideFrom": {"legend": False, "tooltip": False, "viz": False}}
    fc = base_fc(unit=unit, steps=GREEN_ONLY, custom=custom, color={"mode": "palette-classic"},
                 noValue="No card has gone dispatched -> merged in this window yet")
    return {"id": pid, "title": title, "description": desc, "type": "histogram", "datasource": ds,
            "targets": targets, "gridPos": grid, "fieldConfig": fc,
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True,
                                   "calcs": []}, "bucketOffset": 0,
                        "combine": False, "tooltip": {"mode": "single", "sort": "none"}}}


def second_brain():
    p, y = [], 0
    p.append(text_panel(1, y, "Is the vault growing and healthy?", (
        "Growth is the top line: notes should climb and the Inbox should not. "
        "**Inbox backlog** amber at 5, red at 10 - that is you owing the vault an hour.\n\n"
        "**Health findings** are the freshness and link linters. Errors are broken things; "
        "warnings are ageing things. The job strip at the bottom is the same data as Machinery, "
        "filtered to the vault jobs.")))
    y += 3

    p.append(timeseries(2, "Notes total",
                        "notes_total from the last vault sample of each day.", PG,
                        [pgt("select distinct on (date_trunc('day', ts))"
                             " date_trunc('day', ts) as time,\n"
                             "  (payload->>'notes_total')::int as notes\n"
                             "from events where kind='vault' and $__timeFilter(ts)\n"
                             "order by date_trunc('day', ts), ts desc", fmt="time_series")],
                        gp(7, 12, 0, y), unit="short"))
    p.append(gauge(3, "Inbox backlog",
                   "Notes waiting in the vault Inbox right now. Under 5 green, under 10 amber, "
                   "else red.", PG,
                   [pgt("select (payload->>'inbox')::int as \"inbox\"\n"
                        "from events where kind='vault' order by ts desc limit 1")],
                   gp(7, 5, 12, y), unit="short", mn=0, mx=20,
                   steps=[("green", None), ("orange", 5), ("red", 10)]))
    p.append(barchart(4, "Notes by folder (latest sample)",
                      "Where the notes live, from the by_folder map of the most recent vault "
                      "sample.", PG,
                      [pgt("select k as folder, v::int as notes\n"
                           "from events e, jsonb_each_text(e.payload->'by_folder') as kv(k,v)\n"
                           "where e.kind='vault'"
                           " and e.ts = (select max(ts) from events where kind='vault')\n"
                           "order by notes desc")],
                      gp(7, 7, 17, y), xfield="folder", stack=False))
    y += 7

    p.append(timeseries(5, "Health findings",
                        "Errors and warnings from the freshness and link linters, from the last "
                        "vault sample of each day. Errors are broken references; warnings are "
                        "notes going stale. A gap means that checker did not run that day - the "
                        "series is deliberately not zero-filled, so a gap is never read as clean.",
                        PG,
                        [pgt("select distinct on (date_trunc('day', ts))"
                             " date_trunc('day', ts) as time,\n"
                             "  (payload->>'freshness_errors')::int"
                             " as \"freshness errors\",\n"
                             "  (payload->>'link_errors')::int as \"link errors\",\n"
                             "  (payload->>'freshness_warnings')::int"
                             " as \"freshness warnings\",\n"
                             "  (payload->>'link_warnings')::int"
                             " as \"link warnings\"\n"
                             "from events where kind='vault' and $__timeFilter(ts)\n"
                             "order by date_trunc('day', ts), ts desc", fmt="time_series")],
                        gp(7, 12, 0, y), unit="short"))
    p.append(timeseries(6, "Commits per day",
                        "commits_24h from the last vault sample of each day - how much the vault "
                        "actually changed.", PG,
                        [pgt("select distinct on (date_trunc('day', ts))"
                             " date_trunc('day', ts) as time,\n"
                             "  (payload->>'commits_24h')::int as commits\n"
                             "from events where kind='vault' and $__timeFilter(ts)\n"
                             "order by date_trunc('day', ts), ts desc", fmt="time_series")],
                        gp(7, 6, 12, y), unit="short", draw="bars", fill=80))
    p.append(stat(7, "Dev logs, 7 d",
                  "Dev-log notes written in the last 7 days. 3+ green, 1-2 amber, 0 red - a week "
                  "with no dev log means sessions are not being captured.", PG,
                  [pgt("select (payload->>'devlogs_7d')::int as \"dev logs\"\n"
                       "from events where kind='vault' order by ts desc limit 1")],
                  gp(4, 3, 18, y), unit="short",
                  steps=[("red", None), ("orange", 1), ("green", 3)]))
    p.append(stat(8, "Open to-dos",
                  "Unchecked boxes in the vault's Tasks notes. Under 10 green, under 25 amber, "
                  "else red.", PG,
                  [pgt("select (payload->>'todo_open')::int as \"open to-dos\"\n"
                       "from events where kind='vault' order by ts desc limit 1")],
                  gp(4, 3, 21, y), unit="short",
                  steps=[("green", None), ("orange", 10), ("red", 25)]))
    p.append(stat(9, "Freshness + link errors",
                  "The error half of the linters at the last sample. 0 green, anything above "
                  "amber - it is homework, not an outage. No data means a checker did not run.",
                  PG,
                  [pgt("select (payload->>'freshness_errors')::int\n"
                       "     + (payload->>'link_errors')::int as \"errors\"\n"
                       "from events where kind='vault' order by ts desc limit 1")],
                  gp(3, 6, 18, y + 4), unit="short",
                  steps=[("green", None), ("orange", 1)]))
    y += 7

    jobcase = ",\n".join(
        "  coalesce((select case when j.worst = 0 then 'ok' else 'failed' end from j\n"
        "    where j.d = days.d and j.job = '%s'), 'no run') as \"%s\"" % (jb, jb)
        for jb in VAULT_JOBS)
    jobs_sql = (
        "with days as (select generate_series(date_trunc('day', now() - interval '29 days'),\n"
        "                                     date_trunc('day', now()), interval '1 day') as d),\n"
        " j as (select date_trunc('day', ts) as d, payload->>'job' as job,\n"
        "              max((payload->>'exit')::int) as worst\n"
        "       from events where kind='job' and payload->>'phase'='end'\n"
        "         and ts > now() - interval '30 days' group by 1,2)\n"
        "select days.d as time,\n%s\nfrom days order by 1" % jobcase)
    p.append(timeline(10, "Vault jobs, 30 d",
                      "One lane per vault maintenance job, one cell per day: green exited 0, red "
                      "did not, grey means it did not run.", PG, [pgt(jobs_sql)],
                      gp(8, 24, 0, y), kind="status-history", mappings=STATE_MAP))
    y += 8

    p.append(logs(11, "Vault job logs",
                  "Everything the vault jobs logged in the selected time range.",
                  [lokit("{job=\"vault\"}")], gp(8, 24, 0, y)))
    y += 8
    return dashboard("ops-vault","Second Brain", "5m", "now-30d", p)


NO_SLO_TEXT = "SLOs: none for Capsule components yet (D-7 covers applications)."


def no_slo_panel(pid, y):
    return text_panel(pid, y, NO_SLO_TEXT, "", h=2)


UP_MAPPING = [{"type": "value", "options": {
    "1": {"text": "up", "color": "green", "index": 0},
    "0": {"text": "DOWN", "color": "red", "index": 1}}}]


# Every other panel on the two Capsule dashboards reads the LATEST sample and
# is bounded to the last 30 minutes, so a dead probe empties them rather than
# showing a stale number forever. This tile is the one that says WHY they are
# empty: the age of the obs-probe job's own last run (capsule.job.age_s,
# labels->>'job' = 'obs-probe'). Red past 1200 s = two missed 10-minute ticks,
# the same grace status.sh uses.
PROBE_AGE_SQL = ("select value as \"probe age\" from samples\n"
                 "where service='capsule' and name='capsule.job.age_s'\n"
                 "  and labels->>'job' = 'obs-probe'\n"
                 "  and ts > now() - interval '30 minutes'\n"
                 "order by ts desc limit 1")


PROBE_AGE_DESC = ("Seconds since the obs-probe job last finished, from its own "
                  "capsule.job.age_s sample. Green under 20 m, red past 1200 s (two "
                  "missed 10-minute ticks) - when this is red or empty, every other "
                  "panel here is empty because the probe stopped, not because the "
                  "thing it watches did.")


def probe_age_stat(pid, grid):
    return stat(pid, "Probe age", PROBE_AGE_DESC, PG, [pgt(PROBE_AGE_SQL)], grid,
                unit="s", steps=[("green", None), ("red", 1200)],
                noValue="no probe tick in 30 m")


def capsule_postgres():
    p, y = [], 0
    p.append(text_panel(1, y, "Is the events store healthy?", (
        "Connections, size and throughput of the Postgres instance behind `events` and "
        "`samples`. Watch connections for a leak and WAL bytes for growth that outruns "
        "checkpoints.")))
    y += 3

    p.append(row(2, "Is it up?", y)); y += 1
    p.append(stat(3, "Postgres up",
                  "capsule.container.up for capsule-corp-postgres at the latest probe tick.", PG,
                  [pgt("select value as up from samples\n"
                       "where service='capsule' and name='capsule.container.up'\n"
                       "  and labels->>'container'='capsule-corp-postgres'\n"
                       "  and ts > now() - interval '30 minutes'\n"
                       "order by ts desc limit 1")],
                  gp(4, 6, 0, y), steps=[("red", None), ("green", 1)],
                  color_mode="background", mappings=UP_MAPPING))
    p.append(stat(4, "Connections",
                  "Total capsule.postgres.connections across every state, summing the latest "
                  "row per state.", PG,
                  [pgt("with latest as (\n"
                       "  select distinct on (labels->>'state') labels->>'state' as state, value\n"
                       "  from samples where service='capsule'"
                       " and name='capsule.postgres.connections'\n"
                       "  and ts > now() - interval '30 minutes'\n"
                       "  order by labels->>'state', ts desc)\n"
                       "select sum(value) as connections from latest")],
                  gp(4, 6, 6, y), unit="short"))
    p.append(probe_age_stat(16, gp(4, 6, 12, y)))
    y += 4

    p.append(row(5, "Load", y)); y += 1
    p.append(timeseries(6, "Connections by state",
                        "capsule.postgres.connections, one series per connection state, from "
                        "the probe's 10-minute ticks.", PG,
                        [pgt("select ts as time, labels->>'state' as metric, value from samples\n"
                             "where service='capsule' and name='capsule.postgres.connections'\n"
                             "  and $__timeFilter(ts) order by ts", fmt="time_series")],
                        gp(7, 12, 0, y), unit="short", stack=True))
    p.append(timeseries(7, "Events per hour",
                        "capsule.postgres.events.per_hour, the rows written to `events` in the "
                        "hour before each tick.", PG,
                        [pgt("select ts as time, value as \"events/hour\" from samples\n"
                             "where service='capsule'"
                             " and name='capsule.postgres.events.per_hour'\n"
                             "  and $__timeFilter(ts) order by ts", fmt="time_series")],
                        gp(7, 12, 12, y), unit="short"))
    y += 7

    p.append(row(8, "Storage", y)); y += 1
    p.append(stat(9, "Database size",
                  "capsule.postgres.db.bytes at the latest tick.", PG,
                  [pgt("select value as \"db size\" from samples\n"
                       "where service='capsule' and name='capsule.postgres.db.bytes'\n"
                       "  and ts > now() - interval '30 minutes'\n"
                       "order by ts desc limit 1")],
                  gp(6, 6, 0, y), unit="bytes"))
    p.append(bargauge(10, "Table rows",
                      "capsule.postgres.table.rows at the latest tick, for `events` and "
                      "`samples`.", PG,
                      [pgt("select distinct on (labels->>'table') labels->>'table' as \"table\","
                           " value as rows\n"
                           "from samples where service='capsule'"
                           " and name='capsule.postgres.table.rows'\n"
                           "  and ts > now() - interval '30 minutes'\n"
                           "order by labels->>'table', ts desc")],
                      gp(6, 6, 6, y), unit="short", steps=GREEN_ONLY))
    p.append(bargauge(11, "Table bytes",
                      "capsule.postgres.table.bytes at the latest tick, for `events` and "
                      "`samples`.", PG,
                      [pgt("select distinct on (labels->>'table') labels->>'table' as \"table\","
                           " value as bytes\n"
                           "from samples where service='capsule'"
                           " and name='capsule.postgres.table.bytes'\n"
                           "  and ts > now() - interval '30 minutes'\n"
                           "order by labels->>'table', ts desc")],
                      gp(6, 6, 12, y), unit="bytes", steps=GREEN_ONLY))
    p.append(stat(12, "WAL bytes",
                  "capsule.postgres.wal.bytes (sum of pg_ls_waldir()) at the latest tick.", PG,
                  [pgt("select value as \"WAL bytes\" from samples\n"
                       "where service='capsule' and name='capsule.postgres.wal.bytes'\n"
                       "  and ts > now() - interval '30 minutes'\n"
                       "order by ts desc limit 1")],
                  gp(6, 6, 18, y), unit="bytes"))
    y += 6

    p.append(no_slo_panel(13, y))
    y += 2

    p.append(row(14, "Logs", y)); y += 1
    p.append(logs(15, "Postgres logs",
                  "Fluent Bit lines from the capsule-corp-postgres container.",
                  [lokit('{container="capsule-corp-postgres"}')], gp(8, 24, 0, y)))
    y += 8
    return dashboard("ops-capsule-postgres", "Capsule: Postgres", "5m", "now-24h", p)


def capsule_boards():
    p, y = [], 0
    p.append(text_panel(1, y, "Are the boards moving?", (
        "Card flow and run outcomes across every kanban board the probe can open. A board "
        "with cards piling up in one status, or failures that never clear, needs a look.")))
    y += 3

    p.append(row(2, "Is it up?", y)); y += 1
    p.append(stat(3, "Boards seen",
                  "Distinct boards reporting capsule.board.cards at the latest tick each.", PG,
                  [pgt("with latest as (\n"
                       "  select distinct on (labels->>'board') labels->>'board' as board\n"
                       "  from samples where service='capsule' and name='capsule.board.cards'\n"
                       "  and ts > now() - interval '30 minutes'\n"
                       "  order by labels->>'board', ts desc)\n"
                       "select count(*) as boards from latest")],
                  gp(4, 6, 0, y), unit="short"))
    p.append(stat(4, "Bridge tick age",
                  "capsule.bridge.tick.age_s at the latest probe tick - how long since the "
                  "sprint-sync bridge last ran. Under 15 m green, under 30 m amber, else red.",
                  PG,
                  [pgt("select value as \"tick age\" from samples\n"
                       "where service='capsule' and name='capsule.bridge.tick.age_s'\n"
                       "  and ts > now() - interval '30 minutes'\n"
                       "order by ts desc limit 1")],
                  gp(4, 6, 6, y), unit="s",
                  steps=[("green", None), ("orange", 900), ("red", 1800)]))
    p.append(probe_age_stat(16, gp(4, 6, 12, y)))
    y += 4

    p.append(row(5, "Load", y)); y += 1
    p.append(timeseries(6, "Cards per status per board",
                        "capsule.board.cards, one series per board+status pair, stacked.", PG,
                        [pgt("select ts as time,\n"
                             "  (labels->>'board') || ' ' || (labels->>'status') as metric,"
                             " value\n"
                             "from samples where service='capsule'"
                             " and name='capsule.board.cards'\n"
                             "  and $__timeFilter(ts) order by ts", fmt="time_series")],
                        gp(8, 24, 0, y), unit="short", stack=True))
    y += 8

    p.append(timeseries(7, "Runs per outcome, 24 h",
                        "capsule.board.runs.24h, one series per board+outcome pair, stacked.",
                        PG,
                        [pgt("select ts as time,\n"
                             "  (labels->>'board') || ' ' || (labels->>'outcome') as metric,"
                             " value\n"
                             "from samples where service='capsule'"
                             " and name='capsule.board.runs.24h'\n"
                             "  and $__timeFilter(ts) order by ts", fmt="time_series")],
                        gp(7, 12, 0, y), unit="short", stack=True))
    p.append(timeseries(8, "Run duration p50 / p95, by board",
                        "capsule.board.run.duration_s.p50 and .p95, one series per board per "
                        "quantile.", PG,
                        [pgt("select ts as time, (labels->>'board') || ' p50' as metric, value\n"
                             "from samples where service='capsule'"
                             " and name='capsule.board.run.duration_s.p50'\n"
                             "  and $__timeFilter(ts)\n"
                             "union all\n"
                             "select ts as time, (labels->>'board') || ' p95' as metric, value\n"
                             "from samples where service='capsule'"
                             " and name='capsule.board.run.duration_s.p95'\n"
                             "  and $__timeFilter(ts)\n"
                             "order by 1", fmt="time_series")],
                        gp(7, 12, 12, y), unit="s"))
    y += 7

    p.append(bargauge(9, "Open failures, by board",
                      "capsule.board.failures.open at the latest tick - boards with "
                      "consecutive_failures > 0 right now.", PG,
                      [pgt("select distinct on (labels->>'board') labels->>'board' as board,"
                           " value as failures\n"
                           "from samples where service='capsule'"
                           " and name='capsule.board.failures.open'\n"
                           "  and ts > now() - interval '30 minutes'\n"
                           "order by labels->>'board', ts desc")],
                      gp(6, 24, 0, y), unit="short",
                      steps=[("green", None), ("orange", 1)]))
    y += 6

    p.append(row(10, "Storage", y)); y += 1
    p.append(bargauge(11, "Database bytes, by board",
                      "capsule.board.db.bytes at the latest tick.", PG,
                      [pgt("select distinct on (labels->>'board') labels->>'board' as board,"
                           " value as bytes\n"
                           "from samples where service='capsule'"
                           " and name='capsule.board.db.bytes'\n"
                           "  and ts > now() - interval '30 minutes'\n"
                           "order by labels->>'board', ts desc")],
                      gp(6, 12, 0, y), unit="bytes", steps=GREEN_ONLY))
    p.append(bargauge(12, "WAL bytes, by board",
                      "capsule.board.wal.bytes at the latest tick.", PG,
                      [pgt("select distinct on (labels->>'board') labels->>'board' as board,"
                           " value as bytes\n"
                           "from samples where service='capsule'"
                           " and name='capsule.board.wal.bytes'\n"
                           "  and ts > now() - interval '30 minutes'\n"
                           "order by labels->>'board', ts desc")],
                      gp(6, 12, 12, y), unit="bytes", steps=GREEN_ONLY))
    y += 6

    p.append(no_slo_panel(13, y))
    y += 2

    p.append(row(14, "Logs", y)); y += 1
    p.append(logs(15, "Worker logs",
                  "Fluent Bit lines from every Capsule worker run.",
                  [lokit('{job="capsule-worker"}')], gp(8, 24, 0, y)))
    y += 8
    return dashboard("ops-capsule-boards", "Capsule: Boards", "5m", "now-24h", p)


DASHBOARDS = {"vault.json": second_brain, "capsule-postgres.json": capsule_postgres,
              "capsule-boards.json": capsule_boards}


def write_all(out_dir=None):
    """Render every DASHBOARDS entry into plumbing/. Defaults to OUT (the committed
    dashboards/ directory); pass `out_dir` or set DASHBOARDS_OUT so tests
    never write into the repo."""
    out = Path(out_dir) if out_dir else Path(os.environ.get("DASHBOARDS_OUT", OUT))
    plumbing = PLUMBING if out == OUT else out / "plumbing"
    plumbing.mkdir(parents=True, exist_ok=True)
    for name, fn in DASHBOARDS.items():
        (plumbing / name).write_text(json.dumps(fn(), indent=2) + "\n")
        print("wrote", plumbing.name + "/" + name)
    return out


if __name__ == "__main__":
    write_all()
