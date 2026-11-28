"""Phone summary (uid phone-summary; W351): the whole factory in nine big tiles and one short
list, one column wide so it reads on a phone. Plain English, no pickers. Board state comes from
the Lookout views (v_factory, cap_cards, cap_commits, v_waiting_on_owner, v_run_cost); disk and
the offline test come from the live_health table that maintenance/live_snapshot.py refreshes."""
from build_dashboards import PG, gp, pgt, stat
from build_lookout import frame, ltable, var_era

UID, TITLE, FILE = "phone-summary", "Phone summary", "phone-summary.json"
LIVE = "('implementing', 'validating', 'reviewing', 'guide_ruling', 'korin')"
TODAY = "(%s at time zone 'Asia/Bangkok')::date"
# W361 scope: the capsule-corp board only, and cards
# the owner parked on purpose or that failed too often are shelved, not decisions waiting.
BOARD = "board = 'capsule-corp'"
NOT_PARKED = "coalesce(block_kind, '') not in ('parked', 'parked_by_human', 'failure_limit')"
SQL = {
    # A number, not a word: a stat tile shows only numeric columns by default, so a text answer
    # rendered as "no data". 0 running, 1 late, 2 paused for budget, 3 stopped or no report.
    "running": (
        "select coalesce(max(case state_words when 'running' then 0 when 'late' then 1"
        " when 'paused (budget)' then 2 else 3 end), 3) as \"factory\""
        " from v_factory where board = 'capsule-corp'"),
    "progress": ("select count(*) as \"cards in progress\" from cap_cards"
                 " where board = 'capsule-corp' and (phase in " + LIVE
                 + " or status in ('running', 'review'))"),
    "blocked": ("select count(*) as \"blocked cards\" from cap_cards where " + BOARD
                + " and status = 'blocked' and phase <> 'archived' and " + NOT_PARKED),
    # v_waiting_on_owner (migration 010) already holds only live decisions, refused PRs and PRs
    # ready to merge; this tile scopes it to the capsule-corp board.
    "needs": ("select count(*) as \"needs you\" from v_waiting_on_owner"
              " where product = 'capsule-corp'"),
    "landed": ("select count(*) as \"landed today\" from cap_commits where "
               + TODAY % "committed_at" + " = " + TODAY % "now()"),
    "spend": ("select coalesce(sum(usd_est), 0) as \"spend today\" from v_run_cost"
              " where era = '$era' and " + TODAY % "started_at" + " = " + TODAY % "now()"),
    "disk": "select value as \"disk free\" from live_health where check_name = 'disk'",
    "offline": ("select coalesce((select case when ok is true then 'passing'"
                " when ok is false then 'FAILING' end from live_health"
                " where check_name = 'offline_test' limit 1), 'unknown') as \"offline test\""),
    "unpriced": ("select count(*) as \"runs with no price\" from v_run_cost"
                 " where era = '$era' and " + TODAY % "started_at" + " = " + TODAY % "now()"
                 + " and usd_est is null"),
    "list": ("select item as \"what\", why from v_waiting_on_owner"
             " where product = 'capsule-corp' order by since nulls last limit 8"),
}
MUST_HAVE_DATA = ("running", "progress")
RUN_COLORS = [{"type": "value", "options": {
    "0": {"text": "running", "color": "green", "index": 0},
    "1": {"text": "late", "color": "orange", "index": 1},
    "2": {"text": "paused (budget)", "color": "orange", "index": 2},
    "3": {"text": "stopped", "color": "red", "index": 3}}}]
TEST_COLORS = [{"type": "value", "options": {
    "passing": {"color": "green", "index": 0}, "FAILING": {"color": "red", "index": 1},
    "unknown": {"color": "orange", "index": 2}}}]
# (title, description, SQL key, extra stat arguments)
TILES = [
    ("Factory running?", "Is the capsule-corp tick alive (its last pass is under 90 seconds old)."
     " Green running, amber late or paused for budget, red stopped or no report.", "running",
     {"unit": "none", "mappings": RUN_COLORS}),
    ("Cards in progress", "capsule-corp cards an agent is working on right now (building,"
     " validating, reviewing, guide ruling, Korin, or in review).", "progress",
     {"unit": "short"}),
    ("Blocked", "Scope: capsule-corp board, status blocked, cards you"
     " parked on purpose or that failed too often left out. Amber when any.", "blocked",
     {"unit": "short", "steps": [("green", None), ("orange", 1)]}),
    ("Needs you", "Scope: capsule-corp board. Live cards waiting for your answer (needs input,"
     " decision, missing access, rules disagree), plus PRs ready to merge or that could not be"
     " published. Parked and done cards left out. Red when any.", "needs",
     {"unit": "short", "steps": [("green", None), ("red", 1)]}),
    ("Landed today", "Cards committed since midnight, Bangkok time.", "landed",
     {"unit": "short", "steps": [("text", None), ("green", 1)]}),
    ("Spend today", "Estimated cost of agent runs today at list price. A weight, not a bill.",
     "spend", {"unit": "currencyUSD", "decimals": 2}),
    ("Runs with no price", "Runs today whose cost could not be worked out because the model has"
     " no price yet. Spend today is too low by that much. Amber when any.", "unpriced",
     {"unit": "short", "steps": [("green", None), ("orange", 1)]}),
    ("Disk free", "Free space on the Mac. Red under 25 GB.", "disk",
     {"unit": "decgbytes", "decimals": 0, "steps": [("red", None), ("green", 25)]}),
    ("Offline test", "Whether the factory's offline test passes. Red means failing, amber means"
     " unknown.",
     "offline", {"unit": "string", "mappings": TEST_COLORS, "fields": "/.*/"}),
]


def build():
    p = []
    for i, (title, desc, key, kw) in enumerate(TILES):
        p.append(stat(i + 1, title, desc, PG, [pgt(SQL[key])], gp(3, 24, 0, 3 * i),
                      color_mode="background", noValue="no data", **kw))
    p.append(ltable(20, "What needs you", "The oldest things waiting for your answer. Empty"
                    " means nothing is waiting.", SQL["list"], gp(9, 24, 0, 3 * len(TILES))))
    d = frame(UID, TITLE, p, [var_era()])
    d["refresh"] = "30s"
    return d
