"""Stall supervisor: a ready card that nothing explains (`no_progress` after 10 min, at once).
`stall_remedies` registers `DETECTORS`."""
from capsule import config, db, dispatch, host_guard, idle_lane, unblocked
from capsule.db import guard_get

IDLE_NO_RUN_S = 600


def _unexplained(conn, cfg, now, probes):
    """A ready card, no open run, no pause, and no reason `idle_lane.find` or the known waits
    give, for IDLE_NO_RUN_S: `no_progress` at once (not after 45 min)."""
    if idle_lane.working(conn, now):
        return []
    if idle_lane.any_contract_live(conn, now):
        return []                               # a validator or Mac job runs: it holds no seat
    if idle_lane.find(conn, now, (probes or {}).get("work_root"), idle_s=IDLE_NO_RUN_S):
        return []                               # `lane_idle` names the reason
    if guard_get(conn, "tick_restart_pending")[0]:
        return []
    if host_guard._disk_low(conn):               # the disk gate holds strong cards on purpose
        return []
    active = set(config.epic_branches({"epics": [], **cfg}))     # a legacy card stalls no live lane
    ready = [c for c in conn.execute(
        "SELECT * FROM tasks WHERE status='ready' AND"
        " COALESCE(tier,'strong')='strong'")
        if c["branch_name"] in active and db.open_parent(conn, c["id"]) is None]
    if not ready:
        return []
    mark = conn.execute(
        "SELECT MAX(m) FROM (SELECT MAX(ended_at) m FROM task_runs UNION ALL SELECT MAX(started_at)"
        " FROM task_runs UNION ALL SELECT MAX(created_at) FROM task_events WHERE kind NOT LIKE"
        " 'stall_%' AND kind != 'incident_opened')").fetchone()[0]
    mark = mark or max(c["created_at"] for c in ready)
    if now - mark < IDLE_NO_RUN_S:
        return []
    busy = dispatch.busy_branches(conn)
    holds = {unblocked.dispatcher_hold_reason(conn, c, busy, cfg, (probes or {}).get("home"),
                                              (probes or {}).get("work_root")) for c in ready}
    why = ", ".join(sorted(h for h in holds if h))      # e.g. `needs-file <path>`, `design-brief`
    return [{"class": "no_progress", "subject": cfg.get("board") or "board", "since": mark,
             "evidence": [f"ready: {len(ready)}", why or "no run, no pause, no reason"], "grace": 0,
             "l2_after": 0, "reason": why or "unknown"}]


DETECTORS = (_unexplained,)
