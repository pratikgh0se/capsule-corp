"""Which ready tasks may be claimed this tick (spec §6.7, rows 369, 400)."""
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from capsule import blocks, config, db, design_briefs, epic_clean, host_guard, paths, seats
from capsule import stop_queue
from capsule import workspace
from capsule.runners import mac
from capsule import restart as ladder   # `ready` has its own `restart` local (C07c)


def file_set(row):
    raw = row["file_set"] if "file_set" in row.keys() else None
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return val if isinstance(val, list) else []


def _guard_open(conn, key, now):
    row = conn.execute("SELECT value FROM guard_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return False
    value = json.loads(row[0])
    return value.get("until", 0) > now


def board_paused(conn, now):
    """True while this board's own `pause:board` guard is held (row 483):
    the same check `ready` itself applies before considering any card at all,
    exposed so `capsule.unblocked` can apply the identical "would the dispatcher even
    look at this board right now" gate to its own capacity blocker."""
    return _guard_open(conn, "pause:board", now)


WALL_QUIET = 900    # seconds a host-wall outage's pause keeps the idle checks quiet (D161, S26)


def any_pause(conn, now=None, checks=False):
    """True while `pause:board` or any `pause:account:*` guard is held: a deliberate stop, so the
    `unowned-work-idle` rule (`unowned_idle.check`) stays quiet. With `checks`, a pause
    that a host-wall outage set (its guard carries `wall`, the outage's start) stops counting
    `WALL_QUIET` seconds into the outage: a person has been told by then, the idle checks resume.
    The tick heartbeat's `paused` (Grafana's idle rule) reads it with `checks`, so it says the same.
    A pause with no `wall` (a person's, a rate limit's) counts for as long as it is held. A held
    `weekly_pause` (D234) counts too; a garbled row is not a hold."""
    now = now if now is not None else time.time()
    try:
        weekly = db.guard_get(conn, "weekly_pause")[0].get("held")
    except (ValueError, AttributeError, TypeError):
        weekly = False          # garbled or non-dict row: not a hold
    if weekly:
        return True
    held = (json.loads(raw) for raw, in
            conn.execute("SELECT value FROM guard_state WHERE key LIKE 'pause:%'"))
    return any(g.get("until", 0) > now and not (checks and now - g.get("wall", now) > WALL_QUIET)
               for g in held)


def busy_branches(conn):
    """Every branch with a card currently `running` or in `review` - the read `ready`'s
    serial-within-branch rule (row 400) builds from, exposed so `capsule.unblocked`
    applies the identical gate."""
    return {r["branch_name"] for r in conn.execute(
        "SELECT branch_name FROM tasks WHERE status IN ('running','review')")}


def branch_busy(branch_name, busy_branches):
    """True while `branch_name` itself is already running or in review (row 400's serial-
    within-branch rule) - `ready`'s own first per-card gate, shared so `capsule.unblocked`
    never redefines it."""
    return branch_name in busy_branches


def root(board, home):
    """The board's epic-clone root: what `clone_dirty` reads dirtiness from."""
    return paths.board_dir(board, home) / "work"


def _clone_clean(branch_name, work_root):
    """True only when the epic clone exists under `work_root` with nothing uncommitted."""
    if work_root is None:
        return False
    ws = Path(work_root) / workspace._slug(branch_name)
    try:
        return workspace.gitdir(ws).is_dir() and not workspace._git(
            "status", "--porcelain", *workspace.SCRATCH_EXCLUDE, ws=ws).stdout.strip()
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return False


def clone_dirty(conn, branch_name, card_id, work_root=None):
    """The id of another card on `branch_name` that holds the shared epic clone, else `None`
    (`epic_clean.holders`: running, in review, claimed, or a run newer than the epic's
    latest done card; settled runs on old blocked cards hold nothing). One live card per
    epic clone, whatever the file sets: two cards in one working tree would each commit
    the other's edits. Between two ready holders, only the one with the newest run goes (the
    uncommitted work in the clone is its); a holder with no run ranks below one with a run,
    and holders with no run keep dispatch order. A ready holder that (transitively) waits on
    `card_id` cannot run first, so it is ignored when `work_root` shows the clone has no
    uncommitted changes; a dirty or unreadable clone, or no `work_root`, keeps it holding."""
    holders = epic_clean.holders(conn, branch_name)
    rows = conn.execute(
        "SELECT t.id, t.status, t.priority, t.created_at, (SELECT MAX(r.id) FROM task_runs r"
        " WHERE r.task_id=t.id AND r.profile IS NOT 'blind-tester') AS run FROM tasks t"
        " WHERE t.id IN (%s)" % ",".join("?" * len(holders)), tuple(holders)).fetchall()
    me_holds = card_id in holders
    candidates = any(r["status"] == "ready" and r["id"] != card_id for r in rows)
    waiting = {r[0] for r in conn.execute(     # UNION stops a cycle: every descendant of this card
        "WITH RECURSIVE d(id) AS (SELECT child_id FROM task_links WHERE parent_id=?"
        " UNION SELECT l.child_id FROM task_links l JOIN d ON l.parent_id=d.id) SELECT id FROM d",
        (card_id,))} if candidates and _clone_clean(branch_name, work_root) else set()

    def order(r): return (-(r["run"] or 0), -(r["priority"] or 0), r["created_at"], r["id"])
    mine = next((r for r in rows if r["id"] == card_id), None)
    return next((r["id"] for r in sorted(rows, key=order) if r["id"] != card_id
                 and not (r["status"] == "ready" and r["id"] in waiting)
                 and (r["status"] != "ready" or not me_holds or order(r) < order(mine))), None)


def screen_held(conn, branch_name, card_id):
    """True while `blocks.first_screen`'s own epic-hold guard would stop `ready` taking
    this card back (row 369) - the branch's first screen is held outright, or
    held for `screen-fix` on a *different* card - or the lane's `epic clean backup failed`
    incident is open (the first card got the notice; the rest wait, one notice per lane)."""
    hold = blocks.first_screen(conn, branch_name) or {}
    return (hold.get("state") == "held"
            or (hold.get("state") == "fix" and hold.get("task_id") != card_id)
            or epic_clean.backup_failed_open(conn, branch_name))


def running_count(conn, now):
    """How many `tasks` rows are genuinely, currently running - the one definition both
    this module's own `ready` gate and `capsule.unblocked`'s capacity blocker use."""
    return seats.implementer_count(conn, now)


def resolve_assignee(cfg, assignee):
    """The routed role for `assignee` - `cfg["assignees"][assignee]`, itself a key of
    `cfg["routing"]` - or `None` when `assignee` is unset, names no configured
    assignee, or maps to a role with no routing entry."""
    role = cfg.get("assignees", {}).get(assignee)
    return role if role is not None and role in cfg.get("routing", {}) else None


def _record_deferral(conn, task, roles, live, cap, reason, now):
    """One `review_deferred` event per standing deferral, not one per tick (carries the
    card's latest run id, like every event after a claim)."""
    last = conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
                        (task["id"],)).fetchone()
    if last is not None and last[0] == "review_deferred":
        return
    run = conn.execute("SELECT id, run_id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (task["id"],)).fetchone()
    with db.txn(conn):
        db.event(conn, task["id"], "review_deferred",
                 {"roles": roles, "live": live, "cap": cap, "reason": reason}, now=now,
                 run_pk=run and run["id"], run_id=run and run["run_id"])


def admit_reviews(conn, cfg, home, task, missing, now):
    """The roles of `missing` that may start now: as many as `caps.max_review` and
    `caps.max_heavy` leave free across every board, in order."""
    snap = seats.snapshot(conn, cfg, home, now)
    cap = seats.limits(cfg)[0]
    heavy_cap = seats.limits(cfg)[1]
    free = max(min(cap - snap["review"], heavy_cap - snap["heavy"]), 0)
    reason = "heavy_cap" if heavy_cap - snap["heavy"] < cap - snap["review"] else "review_cap"
    if (reason == "heavy_cap" and config.cap(cfg, "external_heavy")
            and snap["heavy"] < config.cap(cfg, "max_heavy")):
        reason = "heavy_cap_external"      # the cap was cut by `caps.external_heavy`
    if snap["unreadable"]:
        free, reason = 0, "board_unreadable"
    if missing[free:]:      # `live` counts this call's admissions: 2 of 2, not the pre-call 0
        _record_deferral(conn, task, missing[free:], snap["review"] + len(missing[:free]),
                         cap, reason, now)
    return missing[:free]


def hold(tick, conn, cfg, route, now):
    """True when a guide ruling or a Korin run must wait: the route's account (or the
    board) is paused, or no review seat is free. Like a review, it waits in the queue when
    `max_review` or `max_heavy` is reached or a board is unreadable."""
    if tick._paused(conn, route["account"], now):
        return True
    got = seats.snapshot(conn, cfg, tick.home, now)
    review_cap, heavy_cap = seats.limits(cfg)
    return bool(got["unreadable"]) or got["review"] >= review_cap or got["heavy"] >= heavy_cap


def _brief_held(conn, cfg, row, home, now):
    """True while the card needs a design brief (TEAM-23). One `design_brief_missing` event
    naming the path is written per card, ever; the card stays `ready`, so the file's arrival
    frees it on the next call with no human step."""
    path = design_briefs.missing(cfg, row, home)
    if path is None:
        return False
    seen = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='design_brief_missing'",
                        (row["id"],)).fetchone()
    if seen is None:
        with db.txn(conn):
            db.event(conn, row["id"], "design_brief_missing", {"path": str(path)}, now=now)
    return True


NEEDS_RE = re.compile(r"(?im)^[ \t]*needs:[ \t]*(.+?)[ \t]*$")


def needs_missing(cfg, row, home=None):
    """The first path on the card's last `needs:` body line that is not a file in the epic's
    clone, else None (inc_bb1905e8). An absolute path or one with `..` counts as missing."""
    found = NEEDS_RE.findall(row["body"] or "")
    if not found:
        return None
    clone = paths.board_dir(cfg["board"], home) / "work" / workspace._slug(row["branch_name"])
    for path in (p.strip() for p in found[-1].split(",") if p.strip()):
        unsafe = Path(path).is_absolute() or ".." in Path(path).parts
        if unsafe or not (clone / path).is_file():
            return path
    return None


def _needs_held(conn, cfg, row, home, now):
    """True while a `needs:` file is absent in the epic clone. One `needs_missing {path}` event
    per card per path (a changed path writes one new event); the card stays `ready`, so the
    file's arrival frees it on the next call: no run, no block, no incident."""
    path = needs_missing(cfg, row, home)
    if path is None:
        return False
    last = conn.execute("SELECT json_extract(payload, '$.path') FROM task_events WHERE task_id=?"
                        " AND kind='needs_missing' ORDER BY id DESC LIMIT 1",
                        (row["id"],)).fetchone()
    if last is None or last[0] != path:
        with db.txn(conn):
            db.event(conn, row["id"], "needs_missing", {"path": path}, now=now)
    return True


def mac_held(conn, task_id, reason, now):
    """One `mac_lane_held {reason}` event per card per reason (D305): the card stays where it is
    and the hold lifts by itself when the lane can run it. The same reason is not written
    again; a changed reason writes one new event. No block, no failure, no pause."""
    last = conn.execute("SELECT json_extract(payload, '$.reason') FROM task_events WHERE task_id=?"
                        " AND kind='mac_lane_held' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if last is None or last[0] != reason:
        with db.txn(conn):
            db.event(conn, task_id, "mac_lane_held", {"reason": reason}, now=now)


def lane_wait(conn, task_id, why, now):
    """True when a mac card's check could not run because the lane could not run it: `why` is a
    `mac.LaneWait` (the lane's own structured reply: no text is matched). Such a wait is no
    failure, no incident and no block (D305). A busy lane is silent; any other reason is the one
    `mac_lane_held` event."""
    if not isinstance(why, mac.LaneWait) or not mac.card_is_mac(conn, task_id):
        return False
    if why != mac.BUSY:
        mac_held(conn, task_id, str(why), now)
    return True


def tier_down(conn, row, host_tiers, mac_hold, now, checks=False):
    """True when the card's runner tier is not up this pass (it waits `ready`). A mac card says
    why (`mac_hold`, the lane's reason from `MacRunner.unavailable_reason`) in its one event.
    `checks`: the tier its validator's commands need, strong unless the card is a mac card."""
    tier = row["tier"] or "strong"
    if checks and tier != "mac":
        tier = "strong"
    if tier in host_tiers:
        return False
    if tier == "mac" and mac_hold:
        mac_held(conn, row["id"], mac_hold, now)
    return True


def _board_stopped(conn, cfg, now):
    """Paused, held at the weekly line (D234) or restarting (C07c): `ready` is empty."""
    held = (db.guard_get(conn, "weekly_pause")[0] or {}).get("held")    # the blind step asks too
    if held or _guard_open(conn, "pause:board", now):
        return True
    restart = db.guard_get(conn, "tick_restart_pending")[0] or {}
    return (restart.get("fired") or {}).get("board") == cfg["board"] and not restart.get("failed")


def _fix_first(conn, rows):
    """`rows` with the `fix_card` of an open or fix_pending incident first: a red base clears
    only when its fix card runs. Stable (others keep their order); fails open on a read error."""
    try:
        fixes = {r[0] for r in conn.execute(
            "SELECT fix_card FROM incidents WHERE status IN ('open','fix_pending')")}
    except sqlite3.Error:
        return rows
    return sorted(rows, key=lambda r: r["id"] not in fixes)


def ready(conn, cfg, host_tiers, now=None, home=None, mac_hold=None):
    now = now if now is not None else time.time()
    caps = cfg["caps"]
    if running_count(conn, now) >= caps["max_running"]:
        return []
    snap = seats.snapshot(conn, cfg, home, now)       # home None: this board alone
    if snap["unreadable"] or snap["heavy"] >= seats.limits(cfg)[1]:
        return []

    if _board_stopped(conn, cfg, now):
        return []

    busy = busy_branches(conn)

    epics = config.epic_branches(cfg)
    if not epics:
        return []
    placeholders = ",".join("?" * len(epics))
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE status='ready' AND branch_name IN ({placeholders})"
        f" AND NOT {stop_queue.PENDING_SQL}"       # pending stop: skip a ready card with a live run
        " ORDER BY priority DESC, created_at ASC", tuple(epics)).fetchall()
    rows = _fix_first(conn, rows)
    guard_cache = {}
    deps_ready = config.redesign(cfg)["deps_ready"]
    out = []
    for row in rows:
        if branch_busy(row["branch_name"], busy):
            continue

        if deps_ready and db.open_parent(conn, row["id"]) is not None:
            continue                                  # S5: an unlanded dependency holds the card

        if (_brief_held(conn, cfg, row, home, now)           # TEAM-23: waits for its design brief
                or _needs_held(conn, cfg, row, home, now)):  # or for a `needs:` file on the clone
            continue

        if clone_dirty(conn, row["branch_name"], row["id"], root(cfg["board"], home)):
            continue

        if screen_held(conn, row["branch_name"], row["id"]):
            continue

        role = resolve_assignee(cfg, row["assignee"])
        if role is None:
            continue

        if tier_down(conn, row, host_tiers, mac_hold, now):
            continue

        account = ladder.account(conn, cfg, row["id"], role)   # M155: the restart route's own pause
        gkey = f"pause:account:{account}"
        if gkey not in guard_cache:
            guard_cache[gkey] = _guard_open(conn, gkey, now)
        if guard_cache[gkey]:
            continue

        out.append(row)

    return host_guard.gate(conn, out, now)
