"""Factory Overview (home, uid lookout-home; spec 6.2): open it and know in five seconds whether the
factory is fine, what is on the floor, what needs you and what shipped. Q1-Q9."""
from build_dashboards import PG, bargauge, gp, pgt, stat, timeline
from build_lookout import (EXPLORER_LINK, TRACE_LINK, frame, last_report, ltable, partition, var_custom, var_era,
                           var_product)

UID, TITLE, FILE = "lookout-home", "Factory Overview", "home.json"
SINCE = [("since the morning brief", "(select since_ts from v_since)"),
         ("12 h", "now() - make_interval(hours => 12)"), ("24 h", "now() - make_interval(hours => 24)"),
         ("7 d", "now() - make_interval(days => 7)")]
SQL = {
    "factory": ("select board, state_words as \"state\" from v_factory where board in ($product)"
                " order by board"),
    "working": "select count(*) as \"agents\" from v_floor where product in ($product) and run_id is not null",
    "waiting_count": ("select count(*) as \"waiting\" from v_waiting_on_owner"
                      " where product in ($product)"),
    "waiting_oldest": ("select coalesce(extract(epoch from now() - min(since))::bigint, 0) as \"oldest_s\""
                       " from v_waiting_on_owner where product in ($product)"),
    "shipped": ("select (select count(*) from cap_commits where board in ($product) and committed_at >= $since)"
                " + (select count(*) from cap_prs where board in ($product) and merged_at >= $since)"
                " as \"shipped\""),
    "budget": ("select (payload->>'session_pct')::float as \"5-hour window\","
               " (payload->>'week_pct')::float as \"weekly window\""
               " from events where kind = 'usage' and ts > now() - interval '30 minutes' order by ts desc limit 1"),
    "incidents": ("select count(*) as \"open incidents\" from cap_incidents"
                  " where board in ($product) and status not in ('closed', 'superseded')"),
    "floor": ("select product, epic, repo, task_id, title, phase_words as \"doing\", role, model, runner,"
              " elapsed_s, waiting_words as \"waiting on\" from v_floor where product in ($product)"
              " order by elapsed_s desc nulls last"),
    "waiting": ("select product, epic, split_part(item, ':', 1) as task_id, item, why, excerpt, since,"
                " how_to_answer as \"how to answer\", link from v_waiting_on_owner"
                " where product in ($product) order by since asc nulls last"),
    "finished": ("select board as product, task_id, epic, left(sha, 12) as \"commit\","
                 " 'https://github.com/' || repo || '/commit/' || sha as commit_url, validator, reviewer, guide,"
                 " review_rounds as \"rounds\", pr_number as \"pr\", committed_at from cap_commits"
                 " where board in ($product) and committed_at >= $since order by committed_at desc"),
    "prs": ("select board as product, epic, pr_number as \"pr\", url,"
            " case when state = 'draft' then 'draft' else 'ready' end as \"draft or ready\", checks,"
            " cards_in_pr as \"cards\", updated_at from cap_prs where board in ($product)"
            " and state in ('draft', 'open') order by updated_at desc"),
    "who": ("select started_at as \"time\", task_id, role from cap_runs where board in ($product)"
            " and era = '$era' and $__timeFilter(started_at)"
            " union all select ended_at, task_id, 'idle' from cap_runs where board in ($product)"
            " and era = '$era' and ended_at is not null and $__timeFilter(ended_at) order by 1"),
}
MUST_HAVE_DATA = ("factory", "budget")
STATE_COLORS = [{"type": "value", "options": {
    "running": {"color": "green", "index": 0}, "late": {"color": "orange", "index": 1},
    "paused (budget)": {"color": "orange", "index": 2}, "stopped": {"color": "red", "index": 3}}}]


def build():
    factory = stat(1, "Factory", "Is the factory running (Q1)? From v_factory: running under 90 s since the"
                   " last pass, paused when an account is budget-paused, stopped beyond 5 min. One state per"
                   " selected board.", PG, [pgt(SQL["factory"])], gp(4, 4, 0, 0), unit="string",
                   mappings=STATE_COLORS, fields="/^state$/", values=True)
    # M1 (fix round 2): values=True gives every selected board its own tile, but fields
    # "/^state$/" leaves every tile labelled "state" - displayName names each tile by board.
    factory["fieldConfig"]["defaults"]["displayName"] = "${__data.fields.board}"
    p = [
        factory,
        stat(2, "Working now", "Agents on a card right now (v_floor rows with a live run).", PG,
             [pgt(SQL["working"])], gp(4, 3, 4, 0), unit="short"),
        stat(3, "Waiting on you", "Things that need your answer (Q7); red when above 0.", PG,
             [pgt(SQL["waiting_count"])], gp(4, 3, 7, 0), unit="short", steps=[("green", None), ("red", 1)]),
        stat(4, "Oldest wait", "How long the oldest item in Waiting on you has waited.", PG,
             [pgt(SQL["waiting_oldest"])], gp(4, 3, 10, 0), unit="s",
             steps=[("green", None), ("orange", 3600), ("red", 86400)]),
        stat(5, "Shipped since you looked", "Card commits plus merged epic PRs since the chosen moment (Q8).", PG,
             [pgt(SQL["shipped"])], gp(4, 3, 13, 0), unit="short"),
        stat(6, "Open incidents", "Incidents Korin is handling; click through to the Explorer (Q1).", PG,
             [pgt(SQL["incidents"])], gp(4, 4, 16, 0), unit="short", steps=[("green", None), ("orange", 1)],
             links=[{"title": "Explorer", "url": "/d/lookout-explorer"}]),
        last_report(7),
        bargauge(8, "Budget windows", "Limit used in the 5-hour and weekly windows, from the usage meter"
                 " (the truth for limits, D6). Thresholds 60 / 70 / 80.", PG, [pgt(SQL["budget"])],
                 gp(4, 24, 0, 4), unit="percent", mn=0, mx=100,
                 steps=[("green", None), ("yellow", 60), ("orange", 70), ("red", 80)]),
        ltable(9, "On the floor now", "Every open card: product, epic, repo, card, what it is doing, which"
               " agent (role and model), for how long, and what it waits on (Q2-Q5).", SQL["floor"],
               gp(10, 24, 0, 8), units={"elapsed_s": "s"},
               links={"task_id": ("Card story", TRACE_LINK), "epic": ("Epic", EXPLORER_LINK)}),
        ltable(10, "Waiting on you (what to do)", "Oldest first. The how to answer column is the exact command"
               " to run (Q7). The Open link on each row already points at the right place - a card's Trace"
               " page, an epic's PR, or a PR's GitHub page - so `item` itself carries no link (M3: a"
               " one-size TRACE_LINK on `item` would send epic and PR rows to a card that does not exist).",
               SQL["waiting"], gp(8, 24, 0, 18), links={"link": ("Open", "${__data.fields.link}")}),
        ltable(11, "Finished since you looked", "Card commits since the chosen moment, with gate outcomes (Q8).",
               SQL["finished"], gp(8, 24, 0, 26),
               links={"task_id": ("Card story", TRACE_LINK), "commit": ("Commit", "${__data.fields.commit_url}")}),
        ltable(12, "PRs in flight", "Open epic PRs on GitHub: draft or ready, checks, cards (Q6).", SQL["prs"],
               gp(6, 24, 0, 34), links={"pr": ("PR", "${__data.fields.url}"), "epic": ("Epic", EXPLORER_LINK)}),
        partition(timeline(13, "Who worked on what", "One row per card touched in range; the colour is the"
                           " agent role on it.", PG, [pgt(SQL["who"])], gp(8, 24, 0, 40)), "task_id"),
    ]
    templating = [var_product(), var_custom("since", "Since", SINCE), var_era()]
    return frame(UID, TITLE, p, templating)
