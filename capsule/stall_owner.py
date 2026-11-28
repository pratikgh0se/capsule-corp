"""Owner-wait timers (stall-supervisor component page, "After L2"; D339). `step` runs once per
tick pass, after `stall` has paged: set aside a card stall at L2 (`aside:<card>` row, `set_aside`
event; `epic_clean.holders` skips it); decide alone after `decide_after_s` (`may_decide` is the
never list; one `auto_decision` event and a `file` row with the `veto` command; the decision
reaches the next brief through `with_decided`); pass an unseen first screen; release a held Korin
fix card for an idle lane; the patrol and heal heartbeats; the "needs you" note.
`probes` (optional): `patrol_beat_at()`, `heal_beat_at()` (epoch seconds, None when no such job is
installed: no item), `kickstart(label)`. `patrol_beat_at` defaults to `patrol.beat`."""
import hashlib
import json
import os
from pathlib import Path

from capsule import (blocks, config, dispatch, epic_clean, idle_lane, incidents, outbox,
                     workspace)
from capsule.answer_base import ACCEPT_CLOSE_KIND, BLOCK_CLOSE
from capsule.db import event, guard_get, guard_set, txn
from capsule.launch_kick import kickstart  # noqa: F401  (the tick's live probe)
from capsule.stall_page import tag
from capsule.stall_veto import add_parser, handle, veto  # noqa: F401  (the veto command)

ASIDE = epic_clean.ASIDE
ASIDE_CLASSES = ("held_triage", "held_blocked", "held_parked", "owner_wait", "state_orphan",
                 "landing_stuck")
RELEASES = ("release", "release-capsule-fault")
TEXT = {"patrol_silent": "the patrol has not run", "heal_silent": "the heal job has not run"}
DAY, WEEK, IDLE_LANE_S = 86400, 7 * 86400, 1800
FIX_HELD = "SELECT fix_card FROM incidents WHERE fix_card IS NOT NULL"
KORIN = "Korin fix card for incident"
# A human verb is an `intervention` event whose author is not `capsule`: Capsule's own patrol
# entries are not one for any "since the last human verb" count (the queries below say so).
HOLD_Q = ("SELECT payload FROM task_events WHERE task_id=? AND kind IN ("  # noqa: S608
          + ",".join(f"'{k}'" for k in blocks.HOLD_EVENTS) + ") ORDER BY id DESC LIMIT 1")


def hold_reason(conn, task_id):
    """The `reason` of the card's newest hold event, '' when it has none."""
    row = conn.execute(HOLD_Q, (task_id,)).fetchone()
    return str(json.loads(row[0] or "{}").get("reason") or "") if row else ""


def _rec(conn, it):
    return guard_get(conn, f"stall:{it['class']}:{it['subject']}")[0]


def _card(conn, task):
    tid = task if isinstance(task, str) else task["id"]
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def asides(conn):
    """{card id: aside row} of the cards set aside."""
    return {k[len(ASIDE):]: json.loads(v) for k, v in conn.execute(
        "SELECT key, value FROM guard_state WHERE key LIKE ?", (ASIDE + "%",))}


# -- never list ---------------------------------------------------------------------------

def _protected(conn, card, un):
    files = dispatch.file_set(card)
    return (bool(workspace.touches_protected(files))
            or any(f.startswith(p) for f in files for p in un["irreversible_paths"])
            or conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND"
                            " kind='protected_path_refused'", (card["id"],)).fetchone() is not None)


def _decided_since_human(conn, tid):
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='auto_decision' AND id>"
        "COALESCE((SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='intervention' AND"
        " COALESCE(json_extract(payload,'$.author'),'')!='capsule'), 0)",
        (tid, tid)).fetchone() is not None


def never(conn, cfg, card, now):
    """Why Capsule may never lift this card alone (design 9.2.3), else ''. `card` is a row. The
    decide-alone path, the fix release and the patrol's verbs all use it."""
    from capsule import stall            # late: `stall` imports this module
    un = config.unattended(cfg)
    _, kind, payload = stall._hold(conn, card["id"])
    for bad, why in (
            (stall._safety(conn, card["id"]), "a safety finding is never decided alone"),
            (kind == "runaway" and payload.get("trigger") == "owner_rounds", "owner_rounds"),
            (card["block_kind"] == "parked", "a park is the owner's"),
            (card["block_kind"] == "spec_stale", "the spec approval is the owner's"),
            (_protected(conn, card, un), "the card changed a protected path"),
            (dispatch.any_pause(conn, now), "a pause is active")):
        if bad:
            return why
    return ""


def owner_held(conn, card):
    """The card waits for the owner (the stall class `owner_wait`: a decision card, a spec
    approval, a guide `ask-owner` ruling): no patrol verb of Capsule's may lift it."""
    from capsule import stall            # late: `stall` imports this module
    return bool(stall._owner_wait(conn, card, stall._hold(conn, card["id"])))


def may_decide(conn, cfg, task, record, now):
    """`(True, '')`, or `(False, why)` when the never list keeps this card aside. `task` is a
    card id or row; `record` is the stall record (`reversible: False` is a no)."""
    card, un = _card(conn, task), config.unattended(cfg)
    if card is None:
        return False, "no such card"
    week = conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='auto_decision'"
                        " AND created_at>?", (int(now) - WEEK,)).fetchone()[0]
    barred = never(conn, cfg, card, now)
    if barred:
        return False, barred
    for bad, why in (
            (_decided_since_human(conn, card["id"]), "decided alone once since the human verb"),
            (week >= un["decisions_per_week"], "the board's decisions of the week are used"),
            ((record or {}).get("reversible") is False, "the choice is not reversible")):
        if bad:
            return False, why
    return True, ""


# -- decide alone -------------------------------------------------------------------------

def _decision(conn, card):
    closes, live = (*BLOCK_CLOSE, ACCEPT_CLOSE_KIND, "closed"), "{}"
    for kind, payload in conn.execute("SELECT kind, payload FROM task_events WHERE task_id=?"
                                      " ORDER BY id", (card["id"],)):
        live = payload if kind == "decision_requested" else "{}" if kind in closes else live
    return json.loads(live or "{}")


def _idle(conn, now):
    return not (idle_lane.working(conn, now) or conn.execute(
        "SELECT 1 FROM tasks WHERE status='ready'").fetchone())


def _option(conn, un, card, it, rec, now):
    """`(option, source, reversible, wait_s)`; `option` None for no choice; `wait_s` None: never."""
    wait = un["decide_after_s"]
    if _idle(conn, now):
        wait = min(wait, un["decide_idle_s"])
    if "hold: first_screen" in it["evidence"]:
        return "pass", "first_screen", True, un["first_screen_s"]
    doc = _decision(conn, card) if card["request_needs"] == "decision" else {}
    if doc:      # a request older than the card's last close is stale: ignored
        return doc.get("recommended"), "decision", doc.get("reversible") is True, wait
    if rec.get("ruling") == "ask-owner" and rec.get("provisional") in RELEASES:
        return rec["provisional"], "guide", rec.get("reversible") is True, wait
    return None, "none", False, wait


def _no_option(conn, cfg, it, rec, now):
    """A decision request with no recommended option is the asker's defect: one incident line."""
    if rec.get("no_option"):
        return
    line = f"stall: {it['class']}: the request has no recommended option"
    if rec.get("incident"):
        incidents.attach(conn, rec["incident"], [line])
    else:
        rec["incident"] = incidents.open_incident(
            conn, board=cfg.get("board") or "board", kind="hang", task_id=it["subject"],
            key=f"stall: no recommended option: {tag([it['subject']])}", evidence=[line],
            description=f"{cfg.get('board') or 'board'}: {line}", now=now)[0]
    guard_set(conn, f"stall:{it['class']}:{it['subject']}", dict(rec, no_option=True,
              incident=rec["incident"]), now=now)


def _label(conn, card, option, source):
    """The text of the chosen option: a decision card's option label, else a plain phrase."""
    if source == "decision":
        for opt in _decision(conn, card).get("options") or []:
            if isinstance(opt, dict) and opt.get("id") == option:
                return str(opt.get("label") or "")[:300]
    return "the first screen passes" if option == "pass" else "the hold is released"


def _lift(conn, card, option, now):
    """Take the option: release the card, or pass the first screen. True when it applied."""
    if option == "pass":
        return blocks.answer_first_screen(conn, card["id"], "screen-ok",
                                          "unseen for the first-screen limit: passed",
                                          now=now, by="capsule")
    if card["status"] == "triage":
        blocks.reset_failures(conn, card["id"], by="capsule", now=now)
        return True
    return card["status"] == "blocked" and blocks.unblock(conn, card["id"], by="capsule", now=now)


def decision_id(conn, card_id, now):
    """A fresh `auto_decision` id (`ad_` and 4 hex) for this card and time."""
    n, did = 0, None
    while did is None or conn.execute("SELECT 1 FROM task_events WHERE kind='auto_decision' AND"
                                      " json_extract(payload,'$.id')=?", (did,)).fetchone():
        did = "ad_" + hashlib.sha256(f"{card_id}:{now}:{n}".encode()).hexdigest()[:4]
        n += 1
    return did


def _apply(conn, cfg, card, it, rec, got, now):
    option, source = got[0], got[1]
    board = cfg.get("board") or "board"
    did = decision_id(conn, card["id"], now)
    undo = f"python3 -m capsule veto {board} {did}"
    with txn(conn):
        if not _lift(conn, card, option, now):
            return
        if source == "decision":      # the answer entry an owner answer would leave
            event(conn, card["id"], "intervention",
                  {"kind": "answer", "at": now, "author": "capsule", "src": did,
                   "text": f"answered {option} by capsule alone: "
                           f"{_label(conn, card, option, source)} (veto: {undo})"[:1000]},
                  now=now)
        event(conn, card["id"], "auto_decision",
              {"id": did, "class": it["class"], "option": option, "source": source, "undo": undo,
               "basis": f"{source}: stalled {it['age_s'] // 60} min, no answer",
               "label": _label(conn, card, option, source)}, now=now)
        outbox.add(conn, "file", f"capsule decided alone: {card['title'][:80]!r} -> {option}"
                   f" ({source}). It was reversible. To undo, veto: {undo}", now=now)
        guard_set(conn, f"stall:{it['class']}:{it['subject']}", dict(rec, level="decided",
                  decision=did), now=now)


def _decide(conn, cfg, it, now):
    rec, card, un = _rec(conn, it), _card(conn, it["subject"]), config.unattended(cfg)
    if rec is None or card is None:
        return
    option, source, rev, wait = _option(conn, un, card, it, rec, now)
    if wait is None or it["age_s"] < wait:
        return
    if source == "decision" and not option:
        return _no_option(conn, cfg, it, rec, now)
    if option and rev and may_decide(conn, cfg, card, dict(rec, reversible=rev), now)[0]:
        _apply(conn, cfg, card, it, rec, (option, source), now)


# -- set aside, the quiet pages -------------------------------------------------------------

def set_aside(conn, card_id, now, cls="patrol", why="", since=None):
    """Leave the card's lane (`aside:<card>` row, `set_aside` event). False when it already is."""
    key = ASIDE + card_id
    if guard_get(conn, key)[0] is not None:
        return False
    card = _card(conn, card_id)
    question = card["title"][:200] if card else ""
    with txn(conn):
        guard_set(conn, key, {"since": now, "class": cls, "why": why, "question": question},
                  now=now)
        event(conn, card_id, "set_aside", {"class": cls, "why": why, "question": question,
              "since": since or now}, now=now)
    return True


def _aside(conn, it, now):
    why = it["evidence"][0] if it["evidence"] else it["class"]
    with txn(conn):
        if not set_aside(conn, it["subject"], now, it["class"], why, it["since"]):
            return
        rec = _rec(conn, it)
        if rec is not None:
            guard_set(conn, f"stall:{it['class']}:{it['subject']}", dict(rec, level="aside"),
                      now=now)


def _dry(conn, cfg, now):
    """Every card that can be held is aside and nothing runs: one `needs you` Mac note a day."""
    aside, last = asides(conn), (guard_get(conn, "dry:needs_you")[0] or {}).get("at", 0)
    rows = conn.execute("SELECT id FROM tasks WHERE status NOT IN ('done','archived','todo')")
    if not aside or any(r["id"] not in aside for r in rows) or now - last < DAY or (
            idle_lane.working(conn, now)):
        return
    outbox.add(conn, "local", f"{cfg.get('board') or 'board'}: needs you: {len(aside)} cards are"
               " set aside and nothing can run", now=now)
    guard_set(conn, "dry:needs_you", {"at": now}, now=now)


def patrol_label():
    """The patrol job's launchd label (the `*capsule-corp-patrol*` plist in `CAPSULE_LA_DIR`), or
    None when no patrol job is installed."""
    plists = sorted(Path(os.environ.get("CAPSULE_LA_DIR") or Path.home() / "Library"
                         / "LaunchAgents").glob("*capsule-corp-patrol*.plist"))
    return plists[0].stem if plists else None


def _patrol_beat_at():
    """Default `patrol_beat_at` probe: the `at` of `$CAPSULE_HOME/patrol.beat` (unit 7), None when
    no patrol job is installed or the file is unreadable."""
    from capsule import paths
    try:
        return float(json.loads((paths.home() / "patrol.beat").read_text())["at"]) \
            if patrol_label() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


SILENT = {"patrol_silent": ("patrol_beat_at", 5400, _patrol_beat_at),
          "heal_silent": ("heal_beat_at", 1200, lambda: None)}


def _kick(conn, it, probes, now):
    """L0 for a silent watcher: one kickstart through the probe per stall."""
    rec, kick = _rec(conn, it), (probes or {}).get("kickstart")
    if rec is None or rec.get("kicked") or kick is None:
        return
    kick((patrol_label() or "patrol") if it["class"] == "patrol_silent" else "heal")
    with txn(conn):
        guard_set(conn, f"stall:{it['class']}:{it['subject']}", dict(rec, kicked=now), now=now)
        event(conn, "stall", "stall_action", {"class": it["class"], "subject": it["subject"],
              "action": "kickstart", "result": "sent"}, now=now)


# -- an idle lane starts a held Korin fix card ----------------------------------------------

def _overlap(a, b):
    return any(x == y or x.startswith(y.rstrip("/") + "/") or y.startswith(x.rstrip("/") + "/")
               for x in a for y in b)


def _lane_idle(conn, branch, now):
    live = conn.execute("SELECT 1 FROM tasks WHERE branch_name=? AND status IN ('ready','running',"
                        "'review','claimed')", (branch,)).fetchone()
    last = conn.execute(
        "SELECT MAX(m) FROM (SELECT MAX(e.created_at) m FROM task_events e JOIN tasks t ON"
        " t.id=e.task_id WHERE t.branch_name=? AND e.kind NOT LIKE 'stall_%' UNION ALL SELECT"
        " MAX(r.ended_at) FROM task_runs r JOIN tasks t ON t.id=r.task_id WHERE t.branch_name=?)",
        (branch, branch)).fetchone()[0]
    return not live and last is not None and now - last >= IDLE_LANE_S


def _released_since_human(conn, task_id):
    """Capsule already lifted this card since the last human verb on it."""
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='unblocked' AND json_extract(payload,"
        "'$.by')='capsule' AND id>COALESCE((SELECT MAX(id) FROM task_events WHERE task_id=? AND"
        " kind='intervention' AND COALESCE(json_extract(payload,'$.author'),'')!='capsule'), 0)",
        (task_id, task_id)).fetchone() is not None


def _release_fix(conn, cfg, un, now):
    """One oldest held fix card whose files are free, for each idle lane, `fix_release_per_day`.
    The fix card goes through the never list too (a protected path, a pause)."""
    fixes = {r[0] for r in conn.execute(FIX_HELD)}
    done = [r[0] for r in conn.execute("SELECT task_id FROM task_events WHERE kind='unblocked'"
                                       " AND created_at>? AND json_extract(payload,'$.by')="
                                       "'capsule'", (now - DAY,))]
    if sum(t in fixes for t in done) >= un["fix_release_per_day"]:
        return
    busy = [dispatch.file_set(c) for c in conn.execute(
        "SELECT * FROM tasks WHERE status IN ('ready','running','review','claimed')")]
    for branch in config.epic_branches({"epics": [], **cfg}):
        if not _lane_idle(conn, branch, now):
            continue
        for card in conn.execute("SELECT * FROM tasks WHERE status='blocked' AND branch_name=?"
                                 " ORDER BY created_at, id", (branch,)).fetchall():
            if (card["id"] in fixes and hold_reason(conn, card["id"]).startswith(KORIN)
                    and not _released_since_human(conn, card["id"])
                    and may_decide(conn, cfg, card, {}, now)[0]
                    and not any(_overlap(dispatch.file_set(card), b) for b in busy)
                    and blocks.unblock(conn, card["id"], by="capsule", now=now)):
                return


# -- the tick's call, and what `stalls` shows ------------------------------------------------

def step(conn, cfg, stalls, now, probes=None):
    """One pass over the open `stalls` (items of `stall.find`): drop the aside rows of cards that
    are no longer stalled, set aside the L2 card stalls, decide alone, the quiet pages."""
    now, live = int(now), {s["subject"] for s in stalls}
    with txn(conn):
        for card, row in asides(conn).items():
            held = conn.execute("SELECT 1 FROM tasks WHERE id=? AND status IN ('triage',"
                                "'blocked')", (card,)).fetchone()
            if card not in live and "class" in row and not held:   # a quiet-stop row stays
                conn.execute("DELETE FROM guard_state WHERE key=?", (ASIDE + card,))
    cards = [s for s in stalls if s["class"] in ASIDE_CLASSES]
    for it in cards:
        if it["level"] == "L2":
            _aside(conn, it, now)
    for it in cards:
        if guard_get(conn, ASIDE + it["subject"])[0] is not None:
            _decide(conn, cfg, it, now)
    for it in stalls:
        if it["class"] in SILENT:
            _kick(conn, it, probes, now)
    _dry(conn, cfg, now)
    _release_fix(conn, cfg, config.unattended(cfg), now)


def show(conn, cfg, items, now):
    """`items` for `stalls --json`: a recorded aside or decided stall shows that level, and a
    `todo` card behind an aside card is listed with the evidence `waits_on_aside`."""
    out = []
    for it in items:
        rec = _rec(conn, it) or {}
        out.append(dict(it, level=rec["level"]) if rec.get("level") in ("aside", "decided") else it)
    for card, row in asides(conn).items():
        for (kid,) in conn.execute("SELECT c.id FROM task_links l JOIN tasks c ON c.id=l.child_id"
                                   " WHERE l.parent_id=? AND c.status='todo'", (card,)):
            out.append({"class": row.get("class", "aside"), "subject": kid,
                        "since": row["since"],
                        "age_s": max(0, int(now) - row["since"]), "level": "watch",
                        "evidence": ["waits_on_aside", f"parent: {card}"]})
    return out
