"""System Now (uid system-now; W347): one live snapshot of the whole system in plain English.
Big coloured tiles for health, then who is working, seats, branches, what is stuck, containers,
what is next and what just finished. Board state comes from the Lookout views (v_floor,
cap_cards, cap_commits); containers, processes, disk, the offline test and seats come from the
live_* tables that maintenance/live_snapshot.py replaces every minute."""
from build_dashboards import PG, gp, pgt, row, stat
from build_lookout import frame, ltable

UID, TITLE, FILE = "system-now", "System Now", "system-now.json"
LIVE = "('implementing', 'validating', 'reviewing', 'guide_ruling', 'korin')"
QUEUED = "phase = 'queued' and status in ('ready', 'todo')"
TRACE = "/d/lookout-trace?var-product=${__data.fields.project}&var-task_id=${__data.fields.card}"
SQL = {
    "parts": (
        "select label, case when ok then 'OK' when check_name = 'offline_test' then 'FAILING'"
        " else 'DOWN' end as \"state\" from live_health where check_name <> 'disk'"
        " order by case when check_name like 'loop:%' then 0"
        " when check_name like 'svc:%' then 1 else 2 end, label"),
    "disk": "select value as \"disk free\" from live_health where check_name = 'disk'",
    "age": (
        "select coalesce(extract(epoch from now() - max(snap_ts))::bigint, 99999)"
        " as \"snapshot age\" from live_health"),
    "running": (
        "select f.product as project, f.task_id as card, f.title as \"card title\","
        " case f.role when 'implementer' then 'builder' when 'reviewer' then 'reviewer'"
        " when 'validator' then 'tester' when 'guide' then 'guide'"
        " when 'korin' then 'incident investigator' else coalesce(f.role, 'unknown') end as role,"
        " coalesce(c.name, 'none found') as container,"
        " now() - f.elapsed_s * interval '1 second' as started, f.elapsed_s as \"running for\""
        " from v_floor f left join live_containers c on c.run_id = f.run_id"
        " or c.name = 'capsule-' || left(f.run_id, 12)"
        " where f.run_id is not null order by f.elapsed_s desc"),
    "building": (
        "select used || ' of ' || coalesce(cap::text, '?') || ' in use' as \"building seats\""
        " from live_seats where seat = 'building'"),
    "review": (
        "select used || ' of ' || coalesce(cap::text, '?') || ' in use' as \"review seats\""
        " from live_seats where seat = 'review'"),
    "keeper": (
        "select case when used > 0 then 'merging now' else 'idle' end as \"merge keeper\""
        " from live_seats where seat = 'keeper'"),
    "lanes": (
        "with lanes as (select board, coalesce(epic, '(no epic)') as epic,"
        " max(case when phase in " + LIVE + " then task_id || ': ' || coalesce(title, '') end)"
        " as live, count(*) filter (where " + QUEUED + ") as queued,"
        " min(waiting_words) filter (where " + QUEUED + ") as why"
        " from cap_cards where phase not in ('done', 'archived') group by 1, 2)"
        " select board as project, epic as branch, coalesce(live, 'nothing running')"
        " as \"live card\", queued as \"queued cards\","
        " case when queued = 0 then 'nothing waiting'"
        " when live is not null then 'waiting: another card is using this branch'"
        " when why like 'waiting for card%' or why like 'retrying%' then 'waiting: ' || why"
        " else 'next in line, starts on the next pass' end as \"why waiting\""
        " from lanes order by board, epic"),
    "blocked": (
        "select board as project, task_id as card, title as \"card title\","
        " case when waiting_on = 'owner' then 'needs you: ' else 'waiting: ' end"
        " || coalesce(waiting_words, status) as \"why\", left(block_reason, 160) as details,"
        " extract(epoch from now() - coalesce(waiting_since, snap_ts))::bigint"
        " as \"waiting for\" from cap_cards"
        " where status in ('blocked', 'triage') and phase <> 'archived' order by 6 desc"),
    "containers": (
        "select name, case kind when 'worker' then 'worker (agent)' else 'service' end"
        " as \"what it is\", image, status,"
        " extract(epoch from now() - created_at)::bigint as \"up for\""
        " from live_containers order by kind, name"),
    "next": (
        "select row_number() over (order by created_at) as \"position\", board as project,"
        " task_id as card, title as \"card title\", coalesce(epic, '(no epic)') as branch"
        " from cap_cards where status = 'ready' and phase = 'queued' order by created_at"),
    "finished": (
        "select c.board as project, c.task_id as card, c.title as \"card title\","
        " coalesce(c.epic, '(no epic)') as branch,"
        " coalesce(m.committed_at, c.completed_at) as finished,"
        " case when m.sha is not null then 'landed' else 'done' end as outcome"
        " from cap_cards c left join lateral (select sha, committed_at from cap_commits m"
        " where m.board = c.board and m.task_id = c.task_id"
        " order by committed_at desc limit 1) m on true"
        " where (c.phase = 'done' and c.completed_at >= now() - interval '6 hours')"
        " or m.committed_at >= now() - interval '6 hours' order by finished desc"),
}
MUST_HAVE_DATA = ("Is everything up?",)
SECTIONS = ("1. Is everything up?", "2. Who is working right now", "3. Seats",
            "4. Branches (lanes)", "5. Stuck or needs a decision", "6. Containers",
            "7. Up next", "8. Finished in the last 6 hours")
STATE_COLORS = [{"type": "value", "options": {
    "OK": {"color": "green", "index": 0}, "DOWN": {"color": "red", "index": 1},
    "FAILING": {"color": "red", "index": 2}}}]
FULL_ORANGE = [
    {"type": "regex", "options": {"pattern": "^(\\d+) of \\1 ",
                                  "result": {"color": "orange", "index": 0}}},
    {"type": "regex", "options": {"pattern": ".*", "result": {"color": "green", "index": 1}}}]
KEEPER_COLORS = [{"type": "value", "options": {"merging now": {"color": "orange", "index": 0},
                                               "idle": {"color": "green", "index": 1}}}]


def _tile(pid, title, desc, key, grid, **kw):
    return stat(pid, title, desc, PG, [pgt(SQL[key])], grid, color_mode="background",
                noValue="no data", **kw)


def _health():
    desc = ("One tile per part: each factory loop, Postgres, Loki, Grafana, Infisical and the"
            " offline test. Green is fine, red needs a look.")
    parts = _tile(2, "Is everything up?", desc, "parts", gp(4, 18, 0, 1), unit="string",
                  mappings=STATE_COLORS, fields="/^state$/", values=True,
                  text_mode="value_and_name")
    parts["fieldConfig"]["defaults"]["displayName"] = "${__data.fields.label}"
    age_desc = ("How old the machine snapshot is. Red when over 3 minutes: the collector has"
                " stopped, so the tiles may be out of date.")
    return [parts,
            _tile(3, "Disk free", "Free space on the Mac. Red under 25 GB.", "disk",
                  gp(4, 3, 18, 1), unit="decgbytes", decimals=0,
                  steps=[("red", None), ("green", 25)]),
            _tile(4, "Snapshot age", age_desc, "age", gp(4, 3, 21, 1), unit="s",
                  steps=[("green", None), ("red", 180)])]


def _seats():
    seat = "Seats in use out of the cap."
    return [_tile(6, "Building seats", seat + " An external seat is not counted.",
                  "building", gp(4, 8, 0, 14), unit="string", mappings=FULL_ORANGE),
            _tile(7, "Review seats", seat, "review", gp(4, 8, 8, 14), unit="string",
                  mappings=FULL_ORANGE),
            _tile(8, "Merge keeper", "Whether a merge keeper holds main right now.", "keeper",
                  gp(4, 8, 16, 14), unit="string", mappings=KEEPER_COLORS)]


# (panel id, title, description, SQL key, y, unit overrides, links, height)
TABLES = [
    (10, "Running agents", "One row per agent running now: project, card, role, container and"
     " how long.", "running", 6, {"running for": "s"}, True, 7),
    (12, "One row per branch", "The card running on each epic branch, how many wait, and why.",
     "lanes", 19, None, False, 7),
    (14, "Blocked cards", "Cards that are blocked or in triage, with the reason and how long ago.",
     "blocked", 27, {"waiting for": "s"}, True, 7),
    (16, "All containers", "Every running container. Services are the stack, workers are agents.",
     "containers", 35, {"up for": "s"}, False, 8),
    (18, "Ready cards in order", "Ready cards in the order the factory takes them (oldest"
     " first).", "next", 44, None, True, 7),
    (20, "Cards finished", "Cards done or landed in the last 6 hours, newest first.",
     "finished", 52, None, True, 7),
]


def _tables():
    link = {"card": ("Card story", TRACE)}
    return [ltable(pid, title, desc, SQL[key], gp(h, 24, 0, y), units=units,
                   links=link if linked else None)
            for pid, title, desc, key, y, units, linked, h in TABLES]


def build():
    ys = (0, 5, 13, 18, 26, 34, 43, 51)
    panels = [row(100 + i, title, y) for i, (title, y) in enumerate(zip(SECTIONS, ys))]
    panels += _health() + _seats() + _tables()
    d = frame(UID, TITLE, panels, [])
    d["refresh"] = "30s"
    d["time"] = {"from": "now-6h", "to": "now"}
    return d
