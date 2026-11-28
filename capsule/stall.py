"""The stall supervisor (docs/designs/stall-supervisor.md): work that can move did not move for
longer than its threshold. One record per stall (`guard_state` `stall:<class>:<subject>`), one
incident (the card's cause incident if it has one), the L2 page (the owner): a `local` outbox row
(Mac notification) each `repage_s`, a Slack page at the start and each `slack_repage_s`, on the
stall clock `now - since`. Classes: held_triage, held_blocked, held_parked, owner_wait,
lane_idle, unroutable, no_progress. `find` is read-only and returns stalls past their grace
time, level `L2` once a stall may page, else `watch`. `step` runs once per tick pass, never
raises, and keeps records without paging or clearing under any pause."""
import json
import re
import sys

from capsule import (blocks, config, db, dispatch, docker_down, epic_clean, exec_outage,
                     host_guard, idle_lane, incidents, interventions, stall_continue, stall_idle,
                     stall_owner, stall_patrol, stall_remedies, stall_ruling, unowned_idle, preview)
from capsule.bundles_guide import ASKS
from capsule.db import event, guard_set, txn
from capsule.stall_page import CARD_EVENT, flush, incident

PREFIX = "stall:"
BOARD_TASK = "stall"            # the task id of an event about the board, not a card
LANE_S, ROUTE_S, PROGRESS_S, WEEK_S = 900, 900, 2700, 7 * 86400
SLOW_AFTER_S = 86400           # from here a stall pages each `slow_repage_s`
RUN_CAP_S = 3900                # an open run is progress until its wall-clock cap has passed
A2_KINDS = ("needs_input", "capability", "transient", "review_infra", "invalid_document",
            "contradiction_loop", "epic_sync_conflict", "epic_sync_error")
HOLDS = ",".join(f"'{k}'" for k in blocks.HOLD_EVENTS)
LIFTS = ",".join(f"'{k}'" for k in blocks.LIFT_EVENTS)
EV = "SELECT created_at, kind, payload FROM task_events WHERE task_id=?"
HOLD_Q = f"{EV} AND kind IN ({HOLDS}) ORDER BY id DESC LIMIT 1"  # noqa: S608
QUIET = "'set_aside','auto_decision','auto_decision_vetoed'"     # Capsule's own, not a hold
NEWEST_Q = f"{EV} AND kind NOT LIKE 'stall_%' AND kind NOT IN ({QUIET}) ORDER BY id DESC LIMIT 1"
CH = "SELECT kind, json_extract(payload, '$.choice') FROM task_events WHERE task_id=?"
ASK_Q = f"{CH} AND kind IN ({HOLDS},{LIFTS},'guide_ruling') ORDER BY id DESC LIMIT 1"  # noqa: S608
BY = "SELECT json_extract(payload, '$.by') FROM task_events WHERE task_id=?"
LIFT_Q = f"{BY} AND kind IN ({LIFTS}) ORDER BY id DESC LIMIT 1"  # noqa: S608
_ID = re.compile(r"\bt_[0-9a-f]{6,}\b")
TITLE_MAX, REASON_MAX = 80, 120
PHRASE = {"held_triage": "stopped", "held_blocked": "is blocked", "held_parked": "is parked",
          "owner_wait": "waits for your answer", "lane_idle": "is ready but cannot start",
          "unroutable": "can never start (no epic or assignee)", **stall_remedies.PHRASE,
          **dict.fromkeys(stall_remedies.RUN_CLASSES, ""), **dict.fromkeys(stall_owner.SILENT, ""),
          **stall_patrol.PHRASE}


def _plain(text, cap):
    return _ID.sub("a card", " ".join(str(text or "").split()))[:cap]


def clock(seconds):
    """`50 min`, or `1 h 05 min` from one hour on."""
    mins = max(0, int(seconds)) // 60
    return f"{mins // 60} h {mins % 60:02d} min" if mins >= 60 else f"{mins} min"


def _hold(conn, task_id):
    """(time, kind, payload dict) of the card's newest hold event; else its newest event."""
    row = conn.execute(HOLD_Q, (task_id,)).fetchone() or conn.execute(
        NEWEST_Q, (task_id,)).fetchone()
    return (row[0], row[1], json.loads(row[2] or "{}")) if row else (None, None, {})


def _waiting(conn, card):
    if not card["branch_name"]:
        return 0
    return conn.execute("SELECT COUNT(*) FROM tasks WHERE branch_name=? AND status='ready'"
                        " AND id != ?", (card["branch_name"], card["id"])).fetchone()[0]


def _safety(conn, task_id):
    """The card's newest reviewer document has a `safety: true` finding (row 642)."""
    row = conn.execute("SELECT handoff FROM task_runs WHERE task_id=? AND profile='reviewer'"
                       " AND handoff IS NOT NULL ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    try:
        return bool(row) and any(f.get("safety") for f in json.loads(row[0]).get("findings", []))
    except (ValueError, AttributeError, TypeError):
        return False


def _owner_wait(conn, card, ev):
    """A hold only the owner can lift (A4)."""
    _, kind, payload = ev
    if card["block_kind"] == "spec_stale" or card["request_needs"] == "decision":
        return True
    if card["block_kind"] == "owner_look":      # F1: owner's only once he was shown the preview
        return preview.asked(conn, card["id"])
    newest = conn.execute(ASK_Q, (card["id"],)).fetchone()
    if newest and newest[0] == "guide_ruling":
        return newest[1] in ASKS
    return kind == "runaway" and (payload.get("trigger") == "owner_rounds" or (
        payload.get("pass") is False and stall_ruling.safety(conn, card["id"])))


def _card_class(conn, card, ev):
    """The stall class of a triage or blocked card, or None when another class owns the hold
    (a Korin fix card waits for a release, a lander wait or an exec outage heals itself)."""
    if _owner_wait(conn, card, ev):
        return "owner_wait"
    if card["status"] == "triage":
        return "held_triage"
    kind, tid = card["block_kind"], card["id"]
    if kind == "parked":
        return "held_parked"
    reason = str(ev[2].get("reason") or "")
    if (kind in A2_KINDS and not reason.startswith("Korin fix card for incident")
            and not blocks.lander_wait(conn, tid) and not exec_outage.held_for_outage(conn, tid)):
        return "held_blocked"
    return None


def _spent(conn, task_id, now):
    """The ladder is spent: a stall ruling or a failed one in the last 24 h (`stall_ruling`). The
    owner is paged at once, not at the deadline."""
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND created_at>? AND ((kind='guide_ruling' AND"
        " json_extract(payload, '$.role')='stall') OR (kind='ruling_failed' AND"
        " json_extract(payload, '$.contradiction') IN (SELECT id FROM task_events WHERE task_id=?"
        " AND kind='stall_ruling')))", (task_id, now - 86400, task_id)).fetchone() is not None


def _lane_cards(conn, now):
    """{card id: lane} for the cards that hold a lane (`epic_clean.lane_holds`)."""
    return {r["id"]: lane for lane in epic_clean.lane_holds(conn, now)
            if not lane.get("unreadable") for r in lane["holders"]}


def _item(cls, subject, since, evidence, grace, l2_after):
    return {"class": cls, "subject": subject, "since": since, "evidence": evidence,
            "grace": grace, "l2_after": l2_after}


def _active(cfg):
    """The board's active lane branches (`epics[]`, the source the tick and `lane_idle` use). The
    card classes ignore a card on any other branch: a legacy card stalls no live lane."""
    return set(config.epic_branches({"epics": [], **cfg}))


def _gate_pending(cfg, probes, card_id, since, now):
    """True while the ask gate (mode `on`) still owes the card a decision or an ask."""
    from pathlib import Path

    from capsule import ask_apply, paths
    home = Path((probes or {}).get("home") or paths.home())
    try:
        return ask_apply.pending(home, cfg.get("board") or "board", cfg, card_id, since, now)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _held_cards(conn, cfg, now, probes):
    """A1, A2, A3 and A4: triage and blocked cards, clocked from the newest hold event."""
    st, lanes, out, active = config.stall(cfg), _lane_cards(conn, now), [], _active(cfg)
    aside = stall_owner.asides(conn)      # a set-aside card left its lane, and stays a stall
    ruling = ("SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL"
              " AND json_extract(COALESCE(metadata, '{}'), '$.ruling_of') IS NOT NULL")
    for card in conn.execute("SELECT * FROM tasks WHERE status IN ('triage','blocked')"):
        if card["branch_name"] not in active:
            continue
        ev = _hold(conn, card["id"])
        cls = _card_class(conn, card, ev) if ev[0] is not None else None
        if cls is None or conn.execute(ruling, (card["id"],)).fetchone():
            continue
        wait = _waiting(conn, card)
        evidence = [f"hold: {ev[1]}", f"waiting: {wait}"]
        spent = _spent(conn, card["id"], now)
        deadline = 0 if spent else st["l2_deadline_s"]
        if cls == "owner_wait":      # a spent ladder pages at once, whatever else runs
            late = spent or wait or not idle_lane.working(conn, now)
            if late and _gate_pending(cfg, probes, card["id"], ev[0], now):
                late = False          # the ask gate owes this card an action: no page yet (B24)
            out.append(_item(cls, card["id"], ev[0], evidence, 0, deadline if late else None))
        elif cls == "held_parked":
            held = card["id"] in aside or (card["id"] in lanes and lanes[card["id"]]["ready"])
            out.append(_item(cls, card["id"], ev[0], evidence, LANE_S if held else WEEK_S,
                             deadline if held else None))
        else:
            out.append(_item(cls, card["id"], ev[0], evidence, st["grace_s"], deadline))
    return out


def _first_screens(conn, cfg, now, probes):
    """A4: an epic held for the owner's first-screen answer."""
    out, active = [], _active(cfg)
    for value, at in conn.execute("SELECT value, updated_at FROM guard_state"
                                  " WHERE key LIKE 'first_screen:%'"):
        held = json.loads(value)
        if isinstance(held, dict) and held.get("state") == "held" and held.get("task_id"):
            card = conn.execute("SELECT branch_name FROM tasks WHERE id=?",
                                (held["task_id"],)).fetchone()
            if not card or card[0] not in active:
                continue
            out.append(_item("owner_wait", held["task_id"], at, ["hold: first_screen"], 0,
                             config.stall(cfg)["l2_deadline_s"]))
    return out


def _lane_idle(conn, cfg, now, probes):
    """B2: ready work, no run starts (`idle_lane.find`: its own 15 min clock)."""
    got = idle_lane.find(conn, now, (probes or {}).get("work_root"),
                         idle_s=None if idle_lane.working(conn, now)
                         else stall_idle.IDLE_NO_RUN_S)
    if not got:
        return []
    item = _item("lane_idle", got["ready"]["id"], now - got["idle_s"],
                 [f"hold: {got['label']}", f"holder: {got['holder']['id']}"], 0,
                 config.stall(cfg)["l2_deadline_s"])
    return [dict(item, holder=got["holder"]["id"], reason=got["label"])]


def _unroutable(conn, cfg, now, probes):
    """B3 (detect only): a ready or todo card that can never start."""
    cfg = {"epics": [], **cfg}
    declared, out = config.epic_branches(cfg), []
    for card in conn.execute("SELECT * FROM tasks WHERE status IN ('ready','todo')"):
        if card["branch_name"] not in declared:
            continue
        gap = interventions.routing_gap(cfg, card["branch_name"], card["assignee"])[2]
        missing = [m for m in gap if m != "assignee" or cfg.get("assignees")]  # bare cfg: unknown
        if card["status"] == "todo" and not db.open_parent(conn, card["id"]):
            wait = card["waits_on_branch"]
            if wait and wait not in declared and not blocks.WAIT_SHA.fullmatch(wait):
                missing = missing + ["branch"]
        if missing:
            since = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=?"
                                 " AND kind NOT LIKE 'stall_%'", (card["id"],)).fetchone()[0]
            out.append(_item("unroutable", card["id"], since or card["created_at"],
                             [f"missing: {', '.join(missing)}"], ROUTE_S,
                             config.stall(cfg)["l2_deadline_s"]))
    return out


def _no_progress(conn, cfg, now, probes):
    """I1: work that could move exists (a card on an epic branch in ready, running, claimed or
    review) and nothing started, ended or changed status for PROGRESS_S. An open run counts as
    progress until its wall-clock cap has passed; while a contract check is live and no
    open run is older than RUN_CAP_S, the limit is `idle_lane.CONTRACT_CAP_S`.
    A board of blocked, parked, triage or todo cards
    only is not this stall: the held classes cover its holds."""
    active = _active(cfg)
    work = [c for c in conn.execute("SELECT id, created_at, branch_name FROM tasks WHERE status IN"
                                    " ('ready','running','review','claimed')")
            if c["branch_name"] in active]
    if not work:
        return []
    mark = conn.execute(
        "SELECT MAX(m) FROM (SELECT MAX(started_at) m FROM task_runs UNION ALL SELECT"
        " MAX(ended_at) FROM task_runs UNION ALL SELECT MAX(created_at) FROM task_events WHERE"
        " kind NOT LIKE 'stall_%' AND kind != 'incident_opened')").fetchone()[0]
    mark = mark or max(c["created_at"] or 0 for c in work)
    stale = conn.execute("SELECT 1 FROM task_runs WHERE ended_at IS NULL AND started_at<?",
                         (now - RUN_CAP_S,)).fetchone()
    contract = idle_lane.any_contract_live(conn, now) and not stale
    if ((idle_lane.working(conn, now) or contract)
            and now - mark < (idle_lane.CONTRACT_CAP_S if contract else RUN_CAP_S)):
        return []
    why = ["no run or status change"]
    if host_guard._disk_low(conn):
        why.append("disk is low (the disk gate holds strong cards)")
    return [_item("no_progress", cfg.get("board") or "board", mark, why, PROGRESS_S, 0)]


def _silent(conn, cfg, now, probes):
    """H2, H3: the patrol or heal heartbeat is older than its limit (`stall_owner.SILENT`). No item
    when the probe is absent or returns None (no such job is installed)."""
    out = []
    for cls, (probe, limit, default) in stall_owner.SILENT.items():
        at = ((probes or {}).get(probe) or default)()
        if at is not None and now - at >= limit:
            out.append(_item(cls, cfg.get("board") or "board", at + limit,
                             [f"last beat {int(now - at) // 60} min ago"], 0, 600))
    return out


DETECTORS = (_held_cards, _first_screens, _unroutable, _silent, *stall_remedies.DETECTORS,
             *stall_patrol.DETECTORS)
FIELDS = ("class", "subject", "since", "age_s", "level", "evidence")


def _guard(det, args, errors):
    """One detector's items; an exception is one stderr line and a name in `errors`."""
    try:
        return det(*args)
    except Exception as exc:
        errors.append(type(exc).__name__)
        print(f"capsule stall: {det.__name__}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []


def _prep(items, now):
    """Items past their grace time, with `age_s` and `level` (`L2` once the stall may page)."""
    out = []
    for it in items:
        age = max(0, now - it["since"])
        if age >= it["grace"]:
            late = it["l2_after"] is not None and age >= it["l2_after"]
            out.append({**it, "age_s": age, "level": "L2" if late else "watch"})
    return out


def _find(conn, cfg, now, probes):
    """(items, errors). A holder card's own stall is its lane's one item: `lane_idle` is only
    for a lane held by something that is not itself a stall."""
    now = int(now)
    if dispatch.any_pause(conn, now):
        return [], []
    errors, args = [], (conn, cfg, now, probes)
    raw = [i for det in DETECTORS for i in _guard(det, args, errors)]
    items = _prep(raw, now)
    mine = {i["subject"] for i in raw}
    items += _prep([i for i in _guard(_lane_idle, args, errors) if i["holder"] not in mine], now)
    if not items:       # rule 23: 10-min `no_progress` only when no other stall explains it
        items += _prep([i for det in stall_idle.DETECTORS for i in _guard(det, args, errors)], now)
    if not [i for i in items if i["l2_after"] is not None]:    # nothing can page
        items += _prep(_guard(_no_progress, args, errors), now)
    return [{**{k: i[k] for k in FIELDS}, **({"reason": i["reason"]} if "reason" in i else {})}
            for i in items], errors


def find(conn, cfg, now, probes=None):
    """Every open stall, read-only: dicts with class, subject, since, age_s, level, evidence.
    `probes` may carry `work_root` (the epic clones, for `lane_idle`)."""
    return stall_owner.show(conn, cfg, _find(conn, cfg, now, probes)[0], now)


# -- records, incidents, pages ------------------------------------------------------------

def _key(item):
    return f"{PREFIX}{item['class']}:{item['subject']}"


def _task(item):
    return item["subject"] if item["class"] in CARD_EVENT else BOARD_TASK


def records(conn):
    """{guard key: record dict} of the open stalls."""
    out = {}
    for key, raw in conn.execute("SELECT key, value FROM guard_state WHERE key LIKE ?",
                                 (PREFIX + "%",)):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        known = (*PHRASE, "no_progress")
        if isinstance(rec, dict) and key[len(PREFIX):].partition(":")[0] in known:
            out[key] = rec
    return out


def _words(conn, cfg, it, age):
    """(plain text, file body with the reply command): the card's title, never its id."""
    head = f"Stalled {clock(age)}"
    card = conn.execute("SELECT * FROM tasks WHERE id=?", (it["subject"],)).fetchone()
    if it["class"] == "no_progress" or card is None:
        what = {**stall_remedies.RUN_TEXT, **stall_owner.TEXT, **stall_patrol.TEXT}.get(
            it["class"], "work waits on the board and nothing has started or finished")
        text = f"{head}: {what}."
        return text, f"capsule: {text}"
    why = _hold(conn, card["id"])[2]
    reason = _plain(stall_ruling.question(conn, card["id"]) or why.get("reason")
                    or why.get("detail") or epic_clean._block_reason(conn, card["id"]), REASON_MAX)
    n = _waiting(conn, card)
    text = (f'{head}: "{_plain(card["title"], TITLE_MAX)}" {PHRASE[it["class"]]}'
            f'{": " + reason if reason else ""}. '
            + (f"{n} card waits." if n == 1 else f"{n} cards wait."))
    held = card["status"] in ("triage", "blocked")
    reply = epic_clean._reply(cfg.get("board") or "board", card) if held else ""
    return text, f"capsule: {text}" + (f" | {reply}" if reply else "")


def _page(conn, cfg, it, rec, now, due):
    """L2: a page is due each `repage_s` on the stall clock; its Slack row at the start and each
    `slack_repage_s`. The page joins `due`; `_flush` sends it. The sound is the first page's."""
    st, slow_s = config.stall(cfg), config.unattended(cfg)["slow_repage_s"]
    slow = slow_s and now - rec["since"] >= SLOW_AFTER_S     # an old stall pages less (D339)
    if it["level"] != "L2" or (rec["paged_at"] and now - rec["paged_at"]
                               < (slow_s if slow else st["repage_s"])):
        return
    text, body = _words(conn, cfg, it, now - rec["since"])
    first = not rec["paged_at"]
    slack_due = first or now - rec["slack_at"] >= (SLOW_AFTER_S if slow else st["slack_repage_s"])
    due.append({"it": it, "text": text, "body": body, "first": first, "slack": slack_due,
                "clock": clock(now - rec["since"])})
    if slack_due:
        rec["slack_at"] = now
    rec["paged_at"], rec["pages"] = now, rec["pages"] + 1


def _advance(conn, cfg, it, rec, now, due):
    """One pass over one stall: open it (record, event, incident) or update it, then page."""
    with txn(conn):
        new = rec is None
        if new:
            rec = {"since": it["since"], "incident": incident(conn, cfg, it, now), "paged_at": 0,
                   "pages": 0, "slack_at": 0}
            event(conn, _task(it), "stall_opened",
                  {"class": it["class"], "subject": it["subject"], "since": it["since"],
                   "incident": rec["incident"]}, now=now)
        before = json.dumps(rec, sort_keys=True)
        if rec.get("level") in ("aside", "decided"):       # set aside: the page stops (D339)
            it = dict(it, level=rec["level"])
        rec.update(level=it["level"], evidence=it["evidence"])
        _page(conn, cfg, it, rec, now, due)
        if new or json.dumps(rec, sort_keys=True) != before:
            guard_set(conn, _key(it), rec, now=now)


def _clear(conn, key, rec, now):
    """The stall is gone: one `stall_cleared` event, the record deleted."""
    cls, _, subject = key[len(PREFIX):].partition(":")
    lift = conn.execute(LIFT_Q, (subject,)).fetchone()
    task = subject if cls in CARD_EVENT else BOARD_TASK
    with txn(conn):
        event(conn, task, "stall_cleared",
              {"class": cls, "subject": subject, "duration_s": max(0, now - rec.get("since", now)),
               "cleared_by": "human" if lift and lift[0] == "human" else "self"}, now=now)
        conn.execute("DELETE FROM guard_state WHERE key=?", (key,))


def _crash(conn, cfg, name, now):
    incidents.open_incident(conn, board=cfg.get("board") or "board", kind="crash",
                            key=f"stall supervisor: {name}", evidence=[f"exception: {name}"],
                            now=now)


def _route_defaults(conn, cfg, now):
    """B3: one declared epic and a ready or todo card with no branch: give it the default branch
    and assignee (what `reset` does), one `epic_assigned` event."""
    cfg = {"epics": [], **cfg}
    for card in conn.execute(      # a card whose epic or assignee was refused stays as it is
            "SELECT * FROM tasks t WHERE status IN ('ready','todo') AND NOT EXISTS (SELECT 1 FROM"
            " task_events e WHERE e.task_id=t.id AND e.kind='epic_refused')"):
        branch, assignee, gap = interventions.routing_gap(
            cfg, card["branch_name"], card["assignee"])
        missing = [m for m in gap if m != "assignee" or cfg.get("assignees")]
        if missing or (branch, assignee) == (card["branch_name"], card["assignee"]):
            continue
        with txn(conn):
            conn.execute("UPDATE tasks SET branch_name=?, assignee=? WHERE id=?",
                         (branch, assignee, card["id"]))
            event(conn, card["id"], "epic_assigned",
                  {"branch": branch, "assignee": assignee}, now=now)


def _run(tick, conn, cfg, now, probes=None):
    root = getattr(tick, "_work_root", None)
    _route_defaults(conn, cfg, now)     # B3 first: a fixed card is no stall
    items, errors = _find(conn, cfg, now,
                          {"work_root": root(cfg) if root else None,
                           "home": getattr(tick, "home", None), **(probes or {})})
    have, live, due = records(conn), set(), []
    for it in items:
        live.add(_key(it))
        try:
            _advance(conn, cfg, it, have.get(_key(it)), now, due)
        except Exception as exc:
            errors.append(type(exc).__name__)
            print(f"capsule stall: {it['class']}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if due:
        try:
            flush(conn, cfg, due, now)
        except Exception as exc:
            errors.append(type(exc).__name__)
            print(f"capsule stall: page: {type(exc).__name__}: {exc}", file=sys.stderr)
    for key, rec in have.items():
        if key not in live and not errors:      # a failed detector must not clear its stalls
            _clear(conn, key, rec, now)
    try:
        stall_remedies.step(tick, conn, cfg, items, now, probes)    # L0: the script remedies
        stall_ruling.step(conn, cfg, items, now)                    # L1: one guide ruling
        if not errors:      # the owner-wait steps need the whole list: a missing item is no clear
            stall_owner.step(conn, cfg, items, now, probes)
    except Exception as exc:
        errors.append(type(exc).__name__)
        print(f"capsule stall: remedy: {type(exc).__name__}: {exc}", file=sys.stderr)
    for name in dict.fromkeys(errors):
        _crash(conn, cfg, name, now)


def step(tick, conn, cfg, strong_up, now, probes=None):
    """The tick's one call: docker-down, the dead-man check, then the stalls. Never raises.
    `probes` (see `stall_remedies`) lets a test inject the process and Docker facts."""
    docker_down.step(conn, cfg, strong_up, now)
    unowned_idle.check(conn, cfg, now)
    try:
        if not dispatch.any_pause(conn, now):
            stall_continue.step(tick, conn, cfg, now)
            _run(tick, conn, cfg, int(now), {"docker_up": strong_up, **(probes or {})}
                 if strong_up is not None else probes)
    except Exception as exc:
        print(f"capsule stall: step failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    host_guard.step(conn, cfg, int(now), probes)    # after the stalls: it may pause the board
