"""Card & Epic Explorer (uid lookout-explorer; spec 6.3): pick a product and epic, see every card
and where work piles up. Q4, Q5, Q6."""
from build_dashboards import PG, bargauge, gp, pgt, stat, timeseries
from build_lookout import (TRACE_LINK, frame, last_report, ltable, var_epic, var_era, var_product,
                           var_query)

UID, TITLE, FILE = "lookout-explorer", "Card & Epic Explorer", "explorer.json"
SCOPE = "board in ($product) and coalesce(epic, '(none)') in ($epic) and status in ($status)"
SQL = {
    "progress": ("select epic || ' - PR ' || coalesce(publish_state, 'none') as \"epic\","
                 " round(100.0 * cards_done / nullif(cards_total, 0), 1) as \"done\" from cap_epics"
                 " where board in ($product) and epic in ($epic) order by epic"),
    "glance": ("select cards_total as \"cards\", cards_blocked as \"blocked\","
               " ahead_of_main as \"ahead of main\", coalesce(pr_number::text, 'no PR') as \"PR\""
               " from cap_epics where board in ($product) and epic in ($epic)"),
    "cards": ("select board as product, task_id, title, status, role_now as role,"
              " (select outcome from cap_runs r where r.board = c.board and r.task_id = c.task_id"
              " and r.era = '$era' order by started_at desc limit 1) as \"last run\","
              " failure_count as failures, review_rounds as rounds, commits,"
              " extract(epoch from now() - created_at)::bigint as age_s, waiting_words as "
              "\"waiting on\""
              " from cap_cards c where " + SCOPE + " order by created_at desc"),
    "piles": ("select status, count(*) as \"cards\" from cap_cards where " + SCOPE +
              " group by status order by 2 desc"),
    "stuck": ("select c.board as product, c.task_id, c.title, b.kind, b.reason, b.waiting_on as "
              "\"waiting on\","
              " b.opened_at as since from cap_blocks b join cap_cards c on c.board = b.board"
              " and c.task_id = b.task_id where b.closed_at is null and c.board in ($product)"
              " and coalesce(c.epic, '(none)') in ($epic) order by b.opened_at asc"),
    "flow": ("select h.ts as time, kv.key as metric, kv.value::float as \"cards\" from events h,"
             " jsonb_each_text(h.payload->'counts') kv where h.kind = 'health' and h.source = "
             "'capsule'"
             " and h.board in ($product) and h.ts > now() - interval '7 days' and "
             "$__timeFilter(h.ts)"
             " and kv.value ~ '^[0-9]+$' order by 1"),
    "incidents": ("select board as product, task_id, kind, status, root_cause, fix_card, opened_at"
                  " from cap_incidents where board in ($product) order by opened_at desc limit 50"),
}
MUST_HAVE_DATA = ("progress", "cards")


def build():
    glance = stat(2, "Epic at a glance", "Cards, blocked, commits ahead of main and PR, per epic.",
                  PG, [pgt(SQL["glance"])], gp(4, 20, 0, 0), unit="short",
                  text_mode="value_and_name")
    glance.update(repeat="epic", repeatDirection="h", maxPerRow=4)
    trace = {"task_id": ("Card story", TRACE_LINK)}
    p = [
        bargauge(1, "Epic progress", "Percent of each epic's cards done; the label shows the PR"
                 " state.", PG, [pgt(SQL["progress"])], gp(8, 24, 0, 4), unit="percent", mn=0,
                 mx=100, steps=[("orange", None), ("green", 100)]),
        glance,
        last_report(3),
        ltable(4, "Every card", "Status of every card in scope (Q4, Q5), newest first.",
               SQL["cards"], gp(12, 24, 0, 12), units={"age_s": "s"}, links=trace),
        bargauge(5, "Where work piles up", "Cards per status; the swelling column is where work"
                 " waits.", PG, [pgt(SQL["piles"])], gp(8, 12, 0, 24), unit="short"),
        ltable(6, "Stuck and why", "Open blocks, oldest first: what is blocked, on whom, since"
               " when.", SQL["stuck"], gp(8, 12, 12, 24), links=trace),
        timeseries(7, "Flow over time", "Cards per status at each health pass, last 7 days."
                   " Is flow getting worse?", PG, [pgt(SQL["flow"], fmt="time_series")],
                   gp(8, 24, 0, 32), unit="short", stack=True),
        ltable(8, "Incidents", "Korin's open and recent incidents.", SQL["incidents"],
               gp(8, 24, 0, 40), links=trace),
    ]
    status = var_query("status", "Status", "select distinct status from cap_cards order by 1")
    status["allValue"] = "select distinct status from cap_cards"
    return frame(UID, TITLE, p, [var_product(), var_epic(), status, var_era()])
