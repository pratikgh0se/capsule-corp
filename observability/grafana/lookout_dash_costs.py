"""Costs & Budgets (uid lookout-costs; spec 6.6): how close the limits are and where the estimated
money went. Q9. Panel SQL reads only v_run_cost, prices and events (the cost views, G6)."""
from build_dashboards import PG, barchart, gauge, gp, pgt, stat, timeseries
from build_lookout import (TRACE_LINK, frame, last_report, ltable, var_era, var_product,
                           var_query)

UID, TITLE, FILE = "lookout-costs", "Costs & Budgets", "costs.json"
RUNS = "board in ($product) and era = '$era' and role in ($role) and model in ($model)"
DAY = "date_trunc('day', started_at at time zone 'Asia/Bangkok')"
BKK = "(%s at time zone 'Asia/Bangkok')::date"
USAGE = ("from events where kind = 'usage' and ts > now() - interval '30 minutes'"
         " order by ts desc limit 1")
SQL = {
    "session": "select (payload->>'session_pct')::float as \"session_pct\" " + USAGE,
    "week": "select (payload->>'week_pct')::float as \"week_pct\" " + USAGE,
    "cost": ("select coalesce(sum(usd_est) filter (where " + BKK % "started_at" + " = " +
             BKK % "now()" + "), 0) as \"today\", coalesce(sum(usd_est), 0) as \"7 days\""
             " from v_run_cost where " + RUNS + " and started_at > now() - interval '7 days'"),
    "by_product": ("select board as product, round(sum(usd_est)::numeric, 2) as \"spend\""
                   " from v_run_cost where " + RUNS + " and $__timeFilter(started_at)"
                   " group by board order by 2 desc"),
    "by_role": ("select coalesce(role, '?') || ' / ' || coalesce(model, '?') as \"role and model\","
                " round(sum(usd_est)::numeric, 2) as \"spend\" from v_run_cost where " + RUNS +
                " and $__timeFilter(started_at) group by 1 order by 2 desc"),
    "expensive": ("select board as product, task_id, round(sum(usd_est)::numeric, 4) as cost_usd,"
                  " count(*) as runs from v_run_cost where " + RUNS +
                  " and $__timeFilter(started_at) group by board, task_id"
                  " order by 3 desc limit 10"),
    "tokens": ("select " + DAY + " as time, coalesce(role, '?') as metric,"
               " sum(coalesce(tokens_in, 0) + coalesce(tokens_out, 0)) as \"tokens\""
               " from v_run_cost where " + RUNS + " and $__timeFilter(started_at)"
               " group by 1, 2 order by 1"),
    "cache": ("select round(100.0 * sum(tokens_cache_read)"
              " / nullif(sum(tokens_in) + sum(tokens_cache_read), 0), 1) as \"cache_pct\""
              " from v_run_cost where " + RUNS + " and $__timeFilter(started_at)"),
    "cache_gap": ("select count(*) as \"runs\" from v_run_cost where " + RUNS +
                  " and tokens_cache_write is null and $__timeFilter(started_at)"),
    "prices": ("select model, valid_from, usd_in_per_mtok as \"in per Mtok\","
               " usd_out_per_mtok as \"out per Mtok\","
               " usd_cache_read_per_mtok as \"cache read per Mtok\","
               " usd_cache_write_per_mtok as \"cache write per Mtok\""
               " from prices order by model, valid_from desc"),
    "other": ("select l.ts as time, s.src as metric, sum((x->>'input')::bigint"
              " + (x->>'output')::bigint) as \"tokens\" from (select distinct on ("
              + BKK % "ts" + ") ts, payload from events where kind = 'ai_usage'"
              " and $__timeFilter(ts) order by " + BKK % "ts" + ", ts desc) l,"
              " lateral (values ('hermes', l.payload->'hermes'),"
              " ('claude code', l.payload->'claude_code')) s(src, arr),"
              " jsonb_array_elements(coalesce(s.arr, '[]'::jsonb)) x group by 1, 2 order by 1"),
}
MUST_HAVE_DATA = ("session", "week")
WINDOW = [("green", None), ("yellow", 60), ("orange", 70), ("red", 80)]


def run_var(name, label, col):
    v = var_query(name, label, "select distinct " + col + " from v_run_cost"
                  " where board in ($product) and era = '$era' and " + col + " is not null"
                  " order by 1")
    v["allValue"] = "select distinct " + col + " from v_run_cost"
    return v


def build():
    est = " (estimate at list price)"
    p = [
        gauge(1, "5-hour window, limit used", "How close the session limit is (Q9).", PG,
              [pgt(SQL["session"])], gp(5, 6, 0, 0), steps=WINDOW),
        gauge(2, "Weekly window, limit used", "How close the week limit is (Q9).", PG,
              [pgt(SQL["week"])], gp(5, 6, 6, 0), steps=WINDOW),
        stat(3, "Factory cost, today and 7 days" + est, "What the agents cost: runs in v_run_cost"
             " priced from the prices table (Q9).", PG, [pgt(SQL["cost"])], gp(5, 8, 12, 0),
             unit="currencyUSD", decimals=2),
        last_report(4),
        barchart(5, "Spend by product" + est, "Where the money goes, per product, in range. The"
                 " epic split needs cards joined in and is left out.", PG,
                 [pgt(SQL["by_product"])], gp(8, 12, 0, 5), unit="currencyUSD", stack=False),
        barchart(6, "Spend by role and model" + est, "Which stage is expensive.", PG,
                 [pgt(SQL["by_role"])], gp(8, 12, 12, 5), unit="currencyUSD", stack=False),
        ltable(7, "Most expensive cards", "Top 10 cards by estimated cost in range.",
               SQL["expensive"], gp(8, 12, 0, 13), units={"cost_usd": "currencyUSD"},
               links={"task_id": ("Card story", TRACE_LINK)}),
        timeseries(8, "Tokens per day by role", "Input plus output tokens per Bangkok day.", PG,
                   [pgt(SQL["tokens"], fmt="time_series")], gp(8, 12, 12, 13), unit="short",
                   stack=True, draw="bars", fill=80),
        stat(9, "Cache share", "Share of prompt tokens read from cache.", PG,
             [pgt(SQL["cache"])], gp(4, 6, 0, 21), unit="percent"),
        stat(10, "Runs missing cache-write tokens", "Above 0 means the cost estimate is"
             " understated for those runs (spec 5.12).", PG, [pgt(SQL["cache_gap"])],
             gp(4, 6, 6, 21), unit="short", steps=[("green", None), ("orange", 1)]),
        ltable(11, "Prices assumed", "Per million tokens, the prices the estimate used.",
               SQL["prices"], gp(4, 12, 12, 21)),
        timeseries(12, "Everything else", "Your own sessions and the old system: tokens/day"
                   " from the usage samples.", PG, [pgt(SQL["other"], fmt="time_series")],
                   gp(8, 24, 0, 25), unit="short", stack=True, draw="bars", fill=80),
    ]
    templating = [var_product(), run_var("role", "Role", "role"),
                  run_var("model", "Model", "model"), var_era()]
    return frame(UID, TITLE, p, templating)
