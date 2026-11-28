"""Agent Trace & Logs (uid lookout-trace; spec 6.5): the story of one card, reached by clicking. Q10."""
from build_dashboards import PG, gp, lokit, logs, pgt, stat, timeline
from build_lookout import frame, last_report, ltable, partition, var_era, var_product, var_query

UID, TITLE, FILE = "lookout-trace", "Agent Trace & Logs", "trace.json"
SQL = {
    "card": ("select c.title, c.board as \"product\", c.epic, c.status, c.waiting_words as \"waiting on\","
             " (select left(sha, 12) from cap_commits m where m.board = c.board and m.task_id = c.task_id"
             " order by committed_at desc limit 1) as \"commit\","
             " (select url from cap_prs p where p.board = c.board and p.epic = c.epic order by updated_at desc"
             " limit 1) as \"pr\" from cap_cards c where c.board = '$product' and c.task_id = '$task_id'"),
    "runs": ("select run_id, role, model, runner, started_at,"
             " extract(epoch from coalesce(ended_at, now()) - started_at)::bigint as duration_s, outcome,"
             " error_class, tokens_in, tokens_out, tokens_cache_read, tokens_cache_write,"
             " round(usd_est::numeric, 4) as cost_usd from v_run_cost where board = '$product'"
             " and task_id = '$task_id' and era = '$era' order by started_at"),
    "timeline": ("select started_at as \"time\", task_id, role from cap_runs where board = '$product'"
                 " and task_id = '$task_id' and era = '$era' union all select ended_at, task_id, 'idle'"
                 " from cap_runs where board = '$product' and task_id = '$task_id' and era = '$era'"
                 " and ended_at is not null order by 1"),
    "story": ("select ts as \"time\", what, actor as \"by\", run_id as \"run\", link from v_card_story"
              " where board = '$product' and task_id = '$task_id' order by ts desc"),
    "incidents": ("select id, kind, status, role, opened_at, closed_at, root_cause, fix_card from cap_incidents"
                  " where board = '$product' and task_id = '$task_id' order by opened_at desc"),
}
LOKI = {
    # I1: run_id has no allValue, so All expands to Grafana's own "(r1|r2|...)" over the runs
    # this card actually has, never ".*" over every board's capsule-worker stream. board=
    # scopes it further even before a run is picked.
    "worker_log": '{job="capsule-worker", board="$product", run_id=~"$run_id"}',
    # M4 (fix round 2, corrected): tick.log and outbox.log are each one file covering every
    # card - fluent-bit (observability/fluent-bit/labels.lua, capsule/lookout-t5-loki 7687b1d)
    # sets task_id as a label only on the per-card worker log path, never on these, so a
    # task_id label match here selects nothing for a real card (round 1's label-matcher fix
    # was backwards). The `|= "$task_id"` line filter is the only way to find a card in a
    # shared file. What keeps an unset $task_id from matching every line is the task_id
    # variable itself (single-value, no All option, no allValue - see
    # test_task_scoped_log_queries_use_line_filter_with_single_value_picker): Grafana always
    # resolves it to a real task_id whenever a card exists, so the empty case happens only on
    # an empty board, where there is nothing to show anyway.
    "tick_log": '{job="capsule-tick"} |= "$task_id"',
    "outbox_log": '{job="capsule-outbox"} |= "$task_id"',
}
MUST_HAVE_DATA = ()


def build():
    p = [
        stat(1, "This card", "Title, product, epic, status, what it waits on, its commit and its PR.", PG,
             [pgt(SQL["card"])], gp(4, 20, 0, 0), unit="string", text_mode="value_and_name", fields="/.*/"),
        last_report(2, multi=False),
        ltable(3, "Runs", "Every agent run on this card (Q10), with tokens and the list-price estimate.",
               SQL["runs"], gp(8, 24, 0, 4), units={"duration_s": "s", "cost_usd": "currencyUSD",
                                                    "tokens_in": "short", "tokens_out": "short",
                                                    "tokens_cache_read": "short", "tokens_cache_write": "short"}),
        partition(timeline(4, "Timeline", "Who worked on this card, in order.", PG, [pgt(SQL["timeline"])],
                           gp(5, 24, 0, 12)), "task_id"),
        ltable(5, "What happened", "The card's story in words, newest first (Q10).", SQL["story"],
               gp(9, 24, 0, 17), links={"link": ("Open", "${__data.fields.link}")}),
        logs(6, "Worker log", "What the agent said, from Loki, for the chosen runs.",
             [lokit(LOKI["worker_log"])], gp(10, 24, 0, 26)),
        logs(7, "Tick log mentions", "Tick log lines that name this card.", [lokit(LOKI["tick_log"])],
             gp(6, 12, 0, 36)),
        logs(8, "Messages sent", "Outbox (Slack and file) lines that name this card.", [lokit(LOKI["outbox_log"])],
             gp(6, 12, 12, 36)),
        ltable(9, "Incidents on this card", "Anything Korin opened on this card.", SQL["incidents"],
               gp(5, 24, 0, 42)),
    ]
    templating = [
        var_product(multi=False),
        var_query("task_id", "Card", "select task_id from cap_cards where board = '$product' order by snap_ts desc",
                  multi=False, include_all=False),
        var_query("run_id", "Run", "select run_id from cap_runs where board = '$product'"
                  " and task_id = '$task_id'"),
        var_era(),
    ]
    return frame(UID, TITLE, p, templating)
