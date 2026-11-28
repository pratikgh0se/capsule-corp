"""Agent costs per card (uid card-costs; W351): what each card cost the agents over the last 7
days, split by stage. Card-centric: the Costs & Budgets dashboard (lookout-costs) already
shows limit windows, spend per product, per role and model and the top cards, so this one does
not repeat them. Reads
v_run_cost (runs priced from the prices table) joined to cap_cards for the card title."""
from build_dashboards import PG, bargauge, gp, pgt, stat
from build_lookout import TRACE_LINK, frame, ltable, var_era

UID, TITLE, FILE = "card-costs", "Agent costs per card", "card-costs.json"
RUNS = "r.era = '$era' and $__timeFilter(r.started_at)"
CARD = "left join cap_cards c on c.board = r.board and c.task_id = r.task_id"
TOK = ("coalesce(r.tokens_in, 0) + coalesce(r.tokens_out, 0)"
       " + coalesce(r.tokens_cache_read, 0) + coalesce(r.tokens_cache_write, 0)")
CAST = "::numeric, 2)"
SQL = {
    "total": "select coalesce(sum(r.usd_est), 0) as \"spend\" from v_run_cost r where " + RUNS,
    "tokens": ("select coalesce(sum(" + TOK + "), 0) as \"tokens\""
               " from v_run_cost r where " + RUNS),
    "cards": ("select count(distinct (r.board, r.task_id)) as \"cards\""
              " from v_run_cost r where " + RUNS),
    "unpriced": ("select count(*) as \"runs with no price\" from v_run_cost r where " + RUNS +
                 " and r.usd_est is null"),
    "top_tokens": ("select r.task_id as \"task_id\", sum(" + TOK + ") as \"tokens\""
                   " from v_run_cost r where " + RUNS +
                   " group by r.board, r.task_id order by 2 desc limit 10"),
    "table": (
        "select r.board as product, r.task_id as task_id, coalesce(c.title, '') as \"card title\","
        " round(sum(r.usd_est)" + CAST + " as \"cost\","
        " round(sum(r.usd_est) filter (where r.role = 'implementer')" + CAST + " as \"builder\","
        " round(sum(r.usd_est) filter (where r.role = 'reviewer')" + CAST + " as \"reviewer\","
        " round(sum(r.usd_est) filter (where r.role = 'validator')" + CAST + " as \"tester\","
        " round(sum(r.usd_est) filter (where r.role not in ('implementer', 'reviewer',"
        " 'validator') or r.role is null)" + CAST + " as \"other roles\","
        " sum(" + TOK + ") as \"tokens\", count(*) as \"runs\""
        " from v_run_cost r " + CARD + " where " + RUNS +
        " group by r.board, r.task_id, c.title order by 4 desc nulls last limit 10"),
}
MUST_HAVE_DATA = ("total",)
MONEY = {"cost": "currencyUSD", "builder": "currencyUSD", "reviewer": "currencyUSD",
         "tester": "currencyUSD", "other roles": "currencyUSD", "tokens": "short"}


def build():
    est = " (estimate at list price)"
    p = [
        stat(1, "Spend, 7 days" + est, "Estimated cost of every agent run in range. A weight,"
             " not a bill.", PG, [pgt(SQL["total"])], gp(4, 6, 0, 0), unit="currencyUSD",
             decimals=2),
        stat(2, "Tokens, 7 days", "Input, output and cache tokens of every run in range.", PG,
             [pgt(SQL["tokens"])], gp(4, 6, 6, 0), unit="short"),
        stat(3, "Cards worked on", "Cards that had at least one agent run in range.", PG,
             [pgt(SQL["cards"])], gp(4, 6, 12, 0), unit="short"),
        stat(4, "Runs with no price", "Runs in range whose cost could not be worked out because"
             " the model has no price yet. Their cost shows as empty, so the spend above is too"
             " low by that much. Amber when any.", PG, [pgt(SQL["unpriced"])], gp(4, 6, 18, 0),
             unit="short", steps=[("green", None), ("orange", 1)]),
        ltable(5, "Where each card's money went", "The ten most expensive cards in range split"
               " by stage: builder, reviewer, tester. Tokens and run count on the right. Click"
               " a card for its story.", SQL["table"], gp(10, 24, 0, 4), units=MONEY,
               links={"task_id": ("Card story", TRACE_LINK)}),
        bargauge(6, "Top 10 cards by tokens", "The ten cards that used the most tokens in range.",
                 PG, [pgt(SQL["top_tokens"])], gp(9, 24, 0, 14), unit="short"),
    ]
    d = frame(UID, TITLE, p, [var_era()])
    d["time"] = {"from": "now-7d", "to": "now"}
    return d
