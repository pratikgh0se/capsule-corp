"""Merges & Delivery (uid lookout-delivery; spec 6.4): did we ship, and was it safe. Q6, Q8."""
from build_dashboards import PG, barchart, gp, histogram, pgt, stat, timeline
from build_lookout import (EXPLORER_LINK, TRACE_LINK, frame, last_report, ltable, partition,
                           var_epic, var_era, var_product)

UID, TITLE, FILE = "lookout-delivery", "Merges & Delivery", "delivery.json"
PR = "board in ($product) and epic in ($epic)"
CM = "board in ($product) and coalesce(epic, '(none)') in ($epic)"
DAY = "date_trunc('day', %s at time zone 'Asia/Bangkok')"
SQL = {
    "merged_week": ("select count(*) as \"epics\" from cap_prs where " + PR +
                    " and merged_at >= now() - interval '7 days'"),
    "delivered": ("select " + DAY % "committed_at" + " as time, count(*) as \"cards\""
                  " from cap_commits where " + CM + " and $__timeFilter(committed_at)"
                  " group by 1 order by 1"),
    "first_pass": ("select round(100.0 * count(*) filter (where review_rounds = 1)"
                   " / nullif(count(*), 0), 1) as \"pass_pct\" from cap_commits where " + CM +
                   " and $__timeFilter(committed_at)"),
    "lead": ("select extract(epoch from m.committed_at - c.created_at) / 3600 as \"lead_h\""
             " from cap_commits m join cap_cards c on c.board = m.board"
             " and c.task_id = m.task_id where m.board in ($product)"
             " and coalesce(m.epic, '(none)') in ($epic) and $__timeFilter(m.committed_at)"),
    "pr_wait": ("select avg(extract(epoch from merged_at - ready_at)) / 3600 as \"wait_h\""
                " from cap_prs where " + PR + " and ready_at is not null"
                " and merged_at is not null and $__timeFilter(merged_at)"),
    "prs": ("select board as product, epic, pr_number as \"pr\", url, state, checks,"
            " review_decision as review, cards_in_pr as cards, opened_at, ready_at, merged_at"
            " from cap_prs where " + PR + " order by updated_at desc"),
    "commits": ("select board as product, task_id, epic, left(sha, 12) as \"commit\","
                " 'https://github.com/' || repo || '/commit/' || sha as commit_url,"
                " validator, reviewer, guide, review_rounds as rounds,"
                " array_to_string(overrides, ', ') as overrides, pr_comment_url"
                " from cap_commits where " + CM + " and $__timeFilter(committed_at)"
                " order by committed_at desc"),
    "reviews": ("select ts as time, task_id, what from v_card_story where board in ($product)"
                " and what in ('review started', 'committed', 'blocked')"
                " and $__timeFilter(ts) order by 1"),
    "per_day": ("select " + DAY % "ts" + " as \"day\","
                " count(*) filter (where k = 'commit') as \"commits\","
                " count(*) filter (where k = 'merge') as \"merges\" from ("
                "select committed_at as ts, 'commit' as k from cap_commits where " + CM +
                " and $__timeFilter(committed_at) union all"
                " select merged_at, 'merge' from cap_prs where " + PR +
                " and $__timeFilter(merged_at)) x group by 1 order by 1"),
}
MUST_HAVE_DATA = ("prs", "commits")
REVIEW_MAP = [{"type": "value", "options": {
    "review started": {"color": "blue", "index": 0}, "committed": {"color": "green", "index": 1},
    "blocked": {"color": "red", "index": 2}}}]


def annotation(name, color, event, tag):
    sql = ("select ts as time, coalesce(title, '" + name + "') as text, '" + tag + "' as tags"
           " from events where kind = 'card' and payload->>'event' = '" + event + "'"
           " and board in ($product)"
           " and $__timeFilter(ts)")
    return {"datasource": PG, "enable": True, "hide": False, "iconColor": color, "name": name,
            "target": {"format": "table", "rawQuery": True, "editorMode": "code", "rawSql": sql}}


def build():
    p = [
        stat(1, "Merged this week", "Epic PRs that landed on main in the last 7 days.", PG,
             [pgt(SQL["merged_week"])], gp(4, 5, 0, 0), unit="short"),
        stat(2, "Cards delivered", "Cards committed in range, with a daily trend.", PG,
             [pgt(SQL["delivered"], fmt="time_series")], gp(4, 5, 5, 0), unit="short",
             graph="area", calc="sum"),
        stat(3, "First-time pass rate", "Share of cards that passed review in one round.", PG,
             [pgt(SQL["first_pass"])], gp(4, 5, 10, 0), unit="percent",
             steps=[("red", None), ("orange", 50), ("green", 80)]),
        stat(4, "PR wait for merge", "Average hours a ready PR waits for your merge.", PG,
             [pgt(SQL["pr_wait"])], gp(4, 5, 15, 0), unit="h", decimals=1,
             steps=[("green", None), ("orange", 24), ("red", 72)]),
        last_report(5),
        histogram(6, "Card lead time", "Hours from a card being created to its commit.", PG,
                  [pgt(SQL["lead"])], gp(8, 24, 0, 4)),
        ltable(7, "Epic PRs", "Every epic PR (Q6): state, checks, review, cards, key dates.",
               SQL["prs"], gp(8, 24, 0, 12),
               links={"pr": ("PR", "${__data.fields.url}"), "epic": ("Epic", EXPLORER_LINK)}),
        ltable(8, "Card commits", "Each card's commit and the gates it passed (Q8).",
               SQL["commits"], gp(9, 24, 0, 20),
               links={"task_id": ("Card story", TRACE_LINK),
                      "commit": ("Commit", "${__data.fields.commit_url}"),
                      "pr_comment_url": ("PR comment", "${__data.fields.pr_comment_url}")}),
        partition(timeline(9, "Review outcomes per card",
                           "One row per card; which cards bounced and where.", PG,
                           [pgt(SQL["reviews"])], gp(8, 24, 0, 29), kind="status-history",
                           mappings=REVIEW_MAP), "task_id"),
        barchart(10, "Delivered per day", "Commits and PR merges per Bangkok day.", PG,
                 [pgt(SQL["per_day"])], gp(8, 24, 0, 37), unit="short", xfield="day"),
    ]
    anns = [annotation("PR merged", "green", "pr_merged", "merge"),
            annotation("Incident opened", "red", "incident_opened", "incident")]
    return frame(UID, TITLE, p, [var_product(), var_epic(), var_era()], annotations=anns)
