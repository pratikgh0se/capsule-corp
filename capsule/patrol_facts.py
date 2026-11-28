"""Patrol facts (docs/architecture/components/patrol.md): the read-only half of the patrol.

`second_look` is the patrol's own oracle (Z2): it reads `tasks`, `task_runs` and `task_events`
with its own SQL and shares no code with `stall.py`. `why` answers "work is ready and nothing
runs: why?" per lane. `work_list` picks the subjects an agent may look at. `collect` builds the
facts of one board. Nothing here writes."""
import json

from capsule import blocks, db, dispatch, epic_clean, idle_lane, stall_owner
from capsule.db import guard_get

MIN_S = 1800                                  # a second look and a `why` need this age
LOOK_GAP_S, LOOKS_MAX, MAX_SUBJECTS, DAY = 6 * 3600, 3, 3, 86400
OWNER_CLASSES = ("owner_wait", "held_parked")  # only the owner can lift these
OWNER_BLOCKS = ("parked", "spec_stale")
QUIET = ("run_empty", "run_asked", "run_early", "run_same_state", "run_harness", "run_silent")
STALL_LEVELS = ("L2", "failed")
HOLDS = ",".join(f"'{k}'" for k in blocks.HOLD_EVENTS)
LIFTS = ",".join(f"'{k}'" for k in blocks.LIFT_EVENTS)
_EV = "SELECT {} FROM task_events WHERE task_id=? AND kind IN ({})"
SINCE_Q = _EV.format("MAX(created_at)", HOLDS)
LIFT_Q = _EV.format("id, created_at", LIFTS) + " AND json_extract(payload, '$.by')='capsule'"


def _since(conn, card):
    row = conn.execute(SINCE_Q, (card["id"],)).fetchone()
    if row[0] is None:
        row = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=?",
                           (card["id"],)).fetchone()
    return row[0] or card["created_at"] or 0


def second_look(conn, now):
    """Stalls the patrol sees by itself, whatever `stalls` says: a held card (triage or blocked, not
    an owner hold) with no open run for MIN_S (`kind: held`), and a lane with ready work, no open
    run and no run start for MIN_S (`kind: idle`, the oldest ready card)."""
    now, out = int(now), []
    live = idle_lane.contract_cards(conn, now)
    for card in conn.execute("SELECT * FROM tasks WHERE status IN ('triage','blocked')"):
        if (card["block_kind"] in OWNER_BLOCKS or card["request_needs"]
                or card["id"] in live):
            continue
        since = _since(conn, card)
        if now - since >= MIN_S and not conn.execute(
                "SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL",
                (card["id"],)).fetchone():
            out.append({"kind": "held", "subject": card["id"], "since": since,
                        "age_s": now - since, "evidence": [f"status: {card['status']}"]})
    for lane in _idle_lanes(conn, now):
        out.append({"kind": "idle", "subject": lane["card"], "since": now - lane["age_s"],
                    "age_s": lane["age_s"], "branch": lane["branch"], "ready": lane["ready"],
                    "evidence": [f"lane: {lane['branch']}", f"ready: {lane['ready']}"]})
    return out


def _idle_lanes(conn, now):
    """Lanes with ready work, no open run and no run start for MIN_S."""
    live = idle_lane.contract_cards(conn, now)
    out = []
    for (branch,) in conn.execute("SELECT DISTINCT branch_name FROM tasks WHERE status='ready'"
                                  " AND branch_name IS NOT NULL ORDER BY branch_name").fetchall():
        ready = [c for c in conn.execute("SELECT * FROM tasks WHERE status='ready' AND"
                                         " branch_name=? ORDER BY created_at, id", (branch,))
                 if db.open_parent(conn, c["id"]) is None]
        runs = conn.execute("SELECT COUNT(*), MAX(r.started_at) FROM task_runs r JOIN tasks t ON"
                            " t.id=r.task_id WHERE t.branch_name=? AND (r.ended_at IS NULL"
                            " OR 1)", (branch,)).fetchone()
        opened = conn.execute("SELECT 1 FROM task_runs r JOIN tasks t ON t.id=r.task_id WHERE"
                              " t.branch_name=? AND r.ended_at IS NULL", (branch,)).fetchone()
        checking = any(r[0] in live for r in conn.execute(
            "SELECT id FROM tasks WHERE branch_name=? AND status NOT IN ('done','archived')",
            (branch,)))
        if not ready or opened or checking:
            continue
        mark = runs[1] or min(c["created_at"] or now for c in ready)
        if now - mark >= MIN_S:
            out.append({"branch": branch, "card": ready[0]["id"], "ready": len(ready),
                        "age_s": now - mark, "runs": runs[0]})
    return out


def _continues(conn, card, now):
    return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind="
                        "'stall_action' AND created_at>? AND json_extract(payload, '$.action')="
                        "'continue'", (card, now - DAY)).fetchone()[0]


def _last_run(conn, branch, now):
    row = conn.execute("SELECT e.task_id, e.payload FROM task_events e JOIN tasks t ON"
                       " t.id=e.task_id WHERE t.branch_name=? AND e.kind='run_closed'"
                       " ORDER BY e.id DESC LIMIT 1", (branch,)).fetchone()
    if row is None:
        return None
    p = json.loads(row[1] or "{}")
    return {"card": row[0], "role": p.get("role"), "outcome": p.get("outcome"),
            "kind": p.get("kind"), "continues": _continues(conn, row[0], now)}


def _holder(conn, lane):
    if not lane or lane.get("unreadable") or lane["live"] or lane["all_ready"]:
        return None
    card = lane["holder"]
    hold = f"{card['status']}/{card['block_kind']}" if card["block_kind"] else card["status"]
    return {"card": card["id"], "hold": hold,
            "aside": bool(guard_get(conn, f"aside:{card['id']}")[0])}


def _reason(row, facts, paused, idle):
    """(reason, action) of one idle lane: the first fixed reason that fits."""
    tick = (facts.get("tick") or {}).get("verdict", "ok")
    holder, last = row["holder"], row["last_run"]
    if paused or tick in ("stopped", "crashloop"):
        return "known_wait", "none"
    if tick in ("wedged", "dead"):
        return "tick", "tick-restart"
    if last and last["kind"] in QUIET and not last["continues"]:
        return "quiet_stop", f"continue {last['card']}"
    if holder:
        return ("all_aside", "none") if holder["aside"] else (
            "holder", f"set-aside {holder['card']}")
    if idle and idle["ready"]["branch_name"] == row["lane"]:
        return idle["hold"], "none"
    return "unknown", "ask-agent"


def why(conn, facts, now):
    """One object per idle lane (ready work, no open run, no run start for MIN_S), each with a
    `reason` from the fixed list and one `action`. Read-only. Only `unknown` needs an agent."""
    now, facts = int(now), facts or {}
    paused = dispatch.any_pause(conn, now)
    lanes = {x["branch"]: x for x in epic_clean.lane_holds(conn, now)}
    idle = idle_lane.find(conn, now, facts.get("work_root"), idle_s=MIN_S)
    out = []
    for lane in (x for x in second_look(conn, now) if x["kind"] == "idle"):
        branch = lane["branch"]
        row = {"lane": branch, "open_runs": 0, "ready": lane["ready"],
               "last_start_age_s": lane["age_s"], "holder": _holder(conn, lanes.get(branch)),
               "last_run": _last_run(conn, branch, now)}
        row["reason"], row["action"] = _reason(row, facts, paused, idle)
        out.append(row)
    return out


def _item(facts, key, cls, subject, reason, age, evidence, waiting=0):
    return {"board": facts.get("board"), "key": key, "class": cls, "subject": subject,
            "reason": reason, "age_s": age, "waiting": waiting, "evidence": evidence}


def _fresh(state, key, now):
    looks = (state.get("looks") or {}).get(key) or []
    return len(looks) < LOOKS_MAX and (not looks or now - max(looks) >= LOOK_GAP_S)


def _missed(facts, state, owner, stalls):
    """Items only the second look sees: 2 patrol runs in a row, at once when `stalls` is broken."""
    seen, out = {s["subject"] for s in stalls}, []
    broken = facts.get("supervisor") == "broken"
    for look in facts.get("second_look") or []:
        sub, missed = look["subject"], (state.get("missed") or {}).get(look["subject"], 0)
        covered = sub in seen or (look["kind"] == "idle" and stalls)
        if sub in owner or (not broken and (covered or missed < 2)):
            continue
        out.append(_item(facts, f"{look['kind']}:{sub}", look["kind"], sub, "missed_by_supervisor",
                         look["age_s"], look["evidence"], (facts.get("waiting") or {}).get(sub, 0)))
    return out


def work_list(facts, state, cfg, now):
    """At most 3 subjects an agent may look at: a `why` question first, then most waiting cards
    first. A stall at L2 or `failed`, old enough (not an owner kind, no guide ruling open); a stall
    only the second look sees (2 patrol runs in a row, at once when the supervisor is broken); an
    unknown idle lane; a tick that stayed bad. `state["missed"]` is `{subject: runs in a row}`,
    `state["looks"]` is `{key: [times]}`."""
    from capsule import config
    state, now, items = state or {}, int(now), []
    min_age = config.patrol(cfg)["min_age_s"]
    owner = set(facts.get("owner_cards") or []) | set(facts.get("ruling") or [])
    owner |= set(facts.get("contract_live") or [])      # a contract check in flight is work
    stalls = facts.get("stalls") or []
    wait = facts.get("waiting") or {}
    for s in stalls:
        if (s["class"] in OWNER_CLASSES or s["subject"] in owner
                or (facts.get("contract_live") and s["class"] in ("no_progress", "lane_idle"))
                or s["level"] not in STALL_LEVELS or s.get("age_s", 0) < min_age):
            continue
        items.append(_item(facts, f"{s['class']}:{s['subject']}", s["class"], s["subject"],
                           "stall", s["age_s"], s.get("evidence") or [],
                           wait.get(s["subject"], 0)))
    items += _missed(facts, state, owner, stalls)
    for lane in facts.get("why") or []:
        if lane["reason"] == "unknown":
            items.append(_item(facts, f"why:{lane['lane']}", "why", lane["lane"], "why_unknown",
                               lane["last_start_age_s"], [f"ready: {lane['ready']}"],
                               lane["ready"]))
    tick = facts.get("tick") or {}
    if (tick.get("verdict", "ok") != "ok" and tick.get("marker") != "valid"
            and tick.get("bad_s", 0) >= min_age):
        items.append(_item(facts, f"tick:{facts.get('board')}", "tick", facts.get("board"),
                           "tick_not_ok", tick["bad_s"], [f"verdict: {tick['verdict']}"]))
    items = [i for i in items if _fresh(state, i["key"], now)]
    order = sorted(items, key=lambda i: (i["class"] != "why", -i["waiting"]))
    return order[:MAX_SUBJECTS]


def cards(conn, now, cfg=None):
    """`{id: facts}` of every card not done or archived, for the document check. `never` is why
    Capsule may not lift the card alone (`stall_owner.never`), else ''."""
    out = {}
    for c in conn.execute("SELECT * FROM tasks WHERE status NOT IN ('done','archived')"):
        cid = c["id"]
        ev = conn.execute(LIFT_Q + " ORDER BY id", (cid,)).fetchall()
        human = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=? AND"
                             " kind='intervention' AND COALESCE(json_extract(payload,"
                             "'$.author'),'')!='capsule'", (cid,)).fetchone()[0]
        out[cid] = {
            "status": c["status"], "block_kind": c["block_kind"], "branch": c["branch_name"],
            "owner": (c["block_kind"] in OWNER_BLOCKS or bool(c["request_needs"])
                      or stall_owner.owner_held(conn, c)),
            "aside": bool(guard_get(conn, f"aside:{cid}")[0]),
            "korin": stall_owner.hold_reason(conn, cid).startswith(stall_owner.KORIN),
            "never": stall_owner.never(conn, cfg, c, now),
            "runs": conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?",
                                 (cid,)).fetchone()[0],
            "releases_24h": sum(1 for _, at in ev if at > now - DAY),
            "releases_since_human": sum(1 for i, _ in ev if i > human),
            "continues_24h": _continues(conn, cid, now)}
    return out
