"""Merge timeline (uid merge-timeline; W351): when each card was built, reviewed, tested and
landed, as a state timeline with one row per card, plus how long cards take to land. Reads
cap_runs (one row per agent run), cap_commits (the landing) and cap_cards (title and start
time). The Merges & Delivery dashboard (lookout-delivery) covers PRs and review outcomes; this
one is the clock."""
from build_dashboards import PG, gp, pgt, stat, timeline
from build_lookout import TRACE_LINK, frame, ltable, partition, var_era

UID, TITLE, FILE = "merge-timeline", "Merge timeline", "merge-timeline.json"
RECENT = ("recent as (select board, task_id from cap_runs where era = '$era'"
          " and $__timeFilter(started_at) group by 1, 2 order by max(started_at) desc limit 12)")
STAGE = ("case r.role when 'implementer' then 'building' when 'reviewer' then 'in review'"
         " when 'validator' then 'testing' when 'guide' then 'guide ruling'"
         " when 'korin' then 'incident check' else 'working' end")
LAND = "coalesce(c.started_at, c.created_at)"
LANDED = ("from cap_commits m join cap_cards c on c.board = m.board and c.task_id = m.task_id"
          " where $__timeFilter(m.committed_at)")
HOURS = "extract(epoch from m.committed_at - " + LAND + ") / 3600"
SQL = {
    "timeline": (
        "with " + RECENT + ", seg as ("
        "select r.started_at as time, r.task_id as card, " + STAGE + " as stage, 1 as ord"
        " from cap_runs r join recent x on x.board = r.board and x.task_id = r.task_id"
        " where r.era = '$era'"
        " union all select coalesce(r.ended_at, now()), r.task_id, 'waiting', 0"
        " from cap_runs r join recent x on x.board = r.board and x.task_id = r.task_id"
        " where r.era = '$era'"
        " union all select m.committed_at, m.task_id, 'landed', 1"
        " from cap_commits m join recent x on x.board = m.board and x.task_id = m.task_id"
        " union all select m.committed_at + interval '20 minutes', m.task_id, 'waiting', 0"
        " from cap_commits m join recent x on x.board = m.board and x.task_id = m.task_id)"
        " select time, card, stage from seg order by time, ord"),
    "median": ("select percentile_cont(0.5) within group (order by " + HOURS + ") as \"median\" " +
               LANDED),
    "slowest": "select max(" + HOURS + ") as \"slowest\" " + LANDED,
    "count": "select count(*) as \"landed\" " + LANDED,
    "table": ("select m.board as product, m.task_id as task_id,"
              " coalesce(c.title, '') as \"card title\","
              " round((" + HOURS + ")::numeric, 1) as \"hours to land\","
              " m.review_rounds as \"review rounds\", m.committed_at as \"landed at\" " + LANDED +
              " order by m.committed_at desc limit 15"),
}
MUST_HAVE_DATA = ("count",)
STAGE_MAP = [{"type": "value", "options": {
    "building": {"color": "blue", "index": 0}, "in review": {"color": "purple", "index": 1},
    "testing": {"color": "yellow", "index": 2}, "guide ruling": {"color": "orange", "index": 3},
    "incident check": {"color": "red", "index": 4}, "working": {"color": "text", "index": 5},
    "landed": {"color": "green", "index": 6}, "waiting": {"color": "#3d3d3d", "index": 7}}}]


def build():
    p = [
        stat(1, "Cards landed", "Cards committed to main in range.", PG, [pgt(SQL["count"])],
             gp(4, 8, 0, 0), unit="short"),
        stat(2, "Typical time to land", "Median hours from a card's first build to its commit.",
             PG, [pgt(SQL["median"])], gp(4, 8, 8, 0), unit="h", decimals=1,
             steps=[("green", None), ("orange", 6), ("red", 24)]),
        stat(3, "Slowest to land", "The longest hours from first build to commit in range.", PG,
             [pgt(SQL["slowest"])], gp(4, 8, 16, 0), unit="h", decimals=1,
             steps=[("green", None), ("orange", 12), ("red", 48)]),
        partition(timeline(4, "Build, review and landing per card", "One row per card, the 12 most"
                           " recent. Blue building, purple in review, yellow testing, green"
                           " landed, grey waiting.", PG, [pgt(SQL["timeline"])], gp(12, 24, 0, 4),
                           mappings=STAGE_MAP, merge=True), "card"),
        ltable(5, "Time to land per card", "The 15 newest landed cards: hours from first build to"
               " commit and how many review rounds it took. Click a card for its story.",
               SQL["table"], gp(9, 24, 0, 16), units={"hours to land": "h"},
               links={"task_id": ("Card story", TRACE_LINK)}),
    ]
    d = frame(UID, TITLE, p, [var_era()])
    d["time"] = {"from": "now-3d", "to": "now"}
    return d
