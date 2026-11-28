"""Stall supervisor, unit 2: the L1 stall ruling (docs/architecture/components/stall-supervisor.md).

One guide ruling per stall of class `held_triage` or `held_blocked`, on the existing ruling path
(`Tick._ruling_one`, schema `decision.v1`): `release`, `release-capsule-fault` or `ask-owner`.
`step` (called by `stall._run`) writes one `stall_ruling` event for an eligible stall; `wanted`
and `pending` let the tick find it; `packet` is what the guide reads; `check` refuses an invalid
document; `apply` is the tick's action. A release is `by='capsule'`: it never refills the
runaway budget, so a card gets exactly one more run. `ask-owner` stores `ruling`, `provisional`
and `reversible` in the stall record (`stall:owner_wait:<card>`, read by `stall_owner`)."""
import json

from capsule import (blocks, cost, dispatch, incidents, outbox, runaway, stall_page,
                     stall_remedies)
from capsule.blind import gate
from capsule.bundles_guide import ASKS, STALL_OPTIONS
from capsule.config import stall as stall_cfg
from capsule.db import event, guard_get, guard_set, txn

KIND = "stall_ruling"
CLASSES = ("held_triage", "held_blocked")
IDS = tuple(i for i, _ in STALL_OPTIONS)
FAULT = {"release": "card", "release-capsule-fault": "capsule"}
DAY, MISSES = 86400, 2
QUESTION = "Whose fault is this hold: the card, or Capsule?"
HOLDS = ",".join(f"'{k}'" for k in blocks.HOLD_EVENTS)
LIFTS = ",".join(f"'{k}'" for k in blocks.LIFT_EVENTS)
EV = "SELECT {} FROM task_events WHERE task_id=?"
HOLD_Q = (EV.format("created_at, kind, payload")  # noqa: S608
          + f" AND kind IN ({HOLDS}) ORDER BY id DESC LIMIT 1")
STATE_Q = (EV.format("kind, json_extract(payload, '$.choice')")  # noqa: S608
           + f" AND kind IN ({HOLDS},{LIFTS},'guide_ruling') ORDER BY id DESC LIMIT 1")
STALLED_Q = (EV.format("1")  # noqa: S608
             + " AND kind='stall_ruling' AND id>COALESCE((SELECT MAX(id)"  # noqa: S608
             f" FROM task_events WHERE task_id=? AND kind IN ({HOLDS},{LIFTS})), 0)")  # noqa: S608
# A `stall_ruling` event waits for its ruling until a ruling, a failure, a hold or a lift follows.
PENDING = ("SELECT e.* FROM task_events e JOIN tasks t ON t.id=e.task_id WHERE"  # noqa: S608
           " t.status IN ('triage','blocked') AND e.kind='stall_ruling' AND NOT EXISTS (SELECT 1"
           " FROM task_events l WHERE l.task_id=e.task_id AND l.id>e.id AND l.kind IN"
           f" ('guide_ruling','ruling_failed',{HOLDS},{LIFTS}))")
RULED = ("FROM task_events WHERE kind='guide_ruling' AND created_at>?"  # noqa: S608
         " AND json_extract(payload, '$.role')='stall'")


def pending(conn, task_id):
    """The card's `stall_ruling` event that still wants the guide, or None."""
    return conn.execute(PENDING + " AND e.task_id=? ORDER BY e.id DESC LIMIT 1",
                        (task_id,)).fetchone()


def wanted(conn):
    """The cards the tick must start a stall ruling run for (oldest first)."""
    return [conn.execute("SELECT * FROM tasks WHERE id=?", (r[0],)).fetchone()
            for r in conn.execute(PENDING.replace("e.*", "DISTINCT e.task_id")
                                  + " ORDER BY e.id")]


def is_stall(conn, ev_id):
    return ev_id is not None and conn.execute(
        "SELECT 1 FROM task_events WHERE id=? AND kind=?", (ev_id, KIND)).fetchone() is not None


def safety(conn, task_id):
    """The card's newest reviewer document has a `safety: true` finding (row 642)."""
    row = conn.execute("SELECT handoff FROM task_runs WHERE task_id=? AND profile='reviewer'"
                       " AND handoff IS NOT NULL ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    try:
        return bool(row) and any(f.get("safety") for f in json.loads(row[0]).get("findings", []))
    except (ValueError, AttributeError, TypeError):
        return False


def _trigger(conn, task_id):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='runaway'"
                       " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(row[0] or "{}").get("trigger") if row else None


def _guide_pass_pending(conn, task):
    """A runaway or claims hold, or a contradiction, whose own guide pass has not been given."""
    if task["block_kind"] == "guide_ruling":
        return True
    ev = runaway.pending_event(conn, task["id"]) if runaway.held(task) else None
    return ev is not None and not conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND id>? AND kind IN"
        " ('guide_ruling','ruling_failed')", (task["id"], ev["id"])).fetchone()


def _releases(conn, task_id, since):
    """Applied stall releases of the card since time `since`."""
    return [r for (r,) in conn.execute(
        "SELECT json_extract(payload, '$.choice') " + RULED + " AND task_id=? AND"
        " json_extract(payload, '$.applied')", (since, task_id)) if r in FAULT]


def eligible(conn, cfg, task, now):
    """(True, why) when a stall ruling may start on this card, else (False, the reason). Never
    for a park, `spec_stale`, a decision card, a safety finding, `owner_rounds`, an `ask-owner`
    ruling, or past a cap (1 release per card per 24 h, 2 since the last human verb, and
    `stall.rulings_per_day` per board per 24 h)."""
    tid = task["id"]
    if task["status"] not in ("triage", "blocked"):
        return False, "the card is not held"
    if (task["block_kind"] in ("parked", "spec_stale", "owner_look")
            or task["request_needs"] == "decision"):
        return False, f"{task['block_kind'] or 'a decision card'}: only the owner lifts it"
    if _trigger(conn, tid) == "owner_rounds" or safety(conn, tid):
        return False, "owner_rounds or a safety finding: the owner rules"
    state = conn.execute(STATE_Q, (tid,)).fetchone()
    if state and state[0] == "guide_ruling" and state[1] in ASKS:
        return False, "the guide already asked the owner"
    if _guide_pass_pending(conn, task):
        return False, "a guide pass is pending"
    if dispatch.any_pause(conn, now):
        return False, "paused"
    if _releases(conn, tid, now - DAY):
        return False, "released in the last 24 h"
    human = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=? AND"
                         " json_extract(payload, '$.by')='human'", (tid,)).fetchone()[0]
    if conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND id>? AND kind="
                    "'guide_ruling' AND json_extract(payload, '$.role')='stall' AND"
                    " json_extract(payload, '$.applied')", (tid, human)).fetchone()[0] >= 2:
        return False, "2 releases since the last human verb"
    cap = stall_cfg(cfg)["rulings_per_day"]
    n = conn.execute("SELECT COUNT(*) " + RULED, (now - DAY,)).fetchone()[0]
    if n >= cap:
        return False, f"{n} stall rulings on the board in 24 h"
    return True, "eligible"


def question(conn, task_id):
    """The guide's own question of its newest `ask-owner` stall ruling on the card, else ''."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='guide_ruling'"
                       " AND json_extract(payload, '$.role')='stall' ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    p = json.loads(row[0] or "{}") if row else {}
    return p.get("question") or "" if p.get("choice") in ASKS else ""


def _stalled_once(conn, task_id):
    """The card already has a `stall_ruling` event since its newest hold or lift."""
    return conn.execute(STALLED_Q, (task_id, task_id)).fetchone() is not None


def step(conn, cfg, items, now):
    """L1: one `stall_ruling` event for each eligible held card, once per stall."""
    for it in items:
        card = conn.execute("SELECT * FROM tasks WHERE id=?", (it["subject"],)).fetchone()
        if it["class"] not in CLASSES or card is None or _stalled_once(conn, card["id"]):
            continue
        if card["block_kind"] == "review_infra" and not stall_remedies.infra_rerun_used(
                conn, card["id"]):
            continue                # L0 (D2) re-runs the review first
        if eligible(conn, cfg, card, now)[0]:
            hold = conn.execute(HOLD_Q, (card["id"],)).fetchone()
            reason = str(json.loads(hold[2] or "{}").get("reason") or "") if hold else ""
            with txn(conn):
                event(conn, card["id"], "stall_ruling",
                      {"class": it["class"], "subject": card["id"], "age_s": it["age_s"],
                       "reason": reason[:300], "pass": True}, now=now)


def _suspect_infra(conn, task_id):
    """True when the only failures of the last round are validator rows that exited 0."""
    def last(profile):
        row = conn.execute("SELECT handoff FROM task_runs WHERE task_id=? AND profile=? AND"
                           " handoff IS NOT NULL ORDER BY id DESC LIMIT 1",
                           (task_id, profile)).fetchone()
        return json.loads(row[0]) if row else {}
    try:
        if last("reviewer").get("findings"):
            return False
        fails = [a for a in gate.scrubbed(last("validator")).get("assertions", [])
                 if a.get("result") == "fail"]
    except (ValueError, AttributeError, TypeError):
        return False
    return bool(fails) and all(a.get("exit") == 0 for a in fails)


def packet(conn, ev):
    """What the guide rules on: the stall, the hold, every finding with the validator exit codes,
    `suspect_infra`, the cost, the earlier rulings, and the D329 question."""
    p, tid = json.loads(ev["payload"]), ev["task_id"]
    hold = conn.execute(HOLD_Q, (tid,)).fetchone()
    trip = json.loads(hold[2] or "{}") if hold else {}
    lines = runaway.finding_lines(conn, tid)
    lines += [f"tripping round: {x}" for x in (trip.get("failures") or "").splitlines()]
    earlier = [f"{q['role']}: {q['choice']}: {q['reason']}"[:300] for q in (
        json.loads(r[0] or "{}") for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='guide_ruling' ORDER BY id",
            (tid,)))]
    return {"kind": "stall", "role": "stall", "class": p.get("class"), "age_s": p.get("age_s"),
            "hold": hold[1] if hold else None, "trigger": trip.get("trigger"),
            "reason": p.get("reason"),
            "findings": [line[:500] for line in lines][-200:],
            "suspect_infra": _suspect_infra(conn, tid), "cost": cost.card_cost(conn, tid),
            "earlier": earlier[-10:], "question": QUESTION}


def check(conn, ev_id, doc):
    """Why a stall ruling document is invalid, else None. `release` and `release-capsule-fault`
    need the note in `context`; `ask-owner` needs `provisional` and `reversible` (rule 11)."""
    if not is_stall(conn, ev_id):
        return None
    if doc["recommended"] in FAULT and not (doc.get("context") or "").strip():
        return "a stall release needs the complete note in `context`"
    if doc["recommended"] not in IDS:
        return f"a stall ruling is one of {', '.join(IDS)}, not {doc['recommended']!r}"
    if doc["recommended"] in ASKS:
        if not ("provisional" in doc and "reversible" in doc):
            return "a stall `ask-owner` needs `provisional` and `reversible`"
        if doc["provisional"] not in (None, "", *FAULT):
            return f"`provisional` is a release ({', '.join(FAULT)}), not {doc['provisional']!r}"
    return None


def record_miss(conn, cfg, task, now, why=""):
    """Count the guide's invalid stall ruling documents of the card's pending ruling: guide runs
    of the card that ended after the `stall_ruling` event (the pending one has no `guide_ruling`
    after it), linked by `metadata.ruling_of` = the event id, or by time when a run has no link
    and no `review_of`. At the second one: a `ruling_failed` event and `failed`
    (the "stall ladder failed" incident, a repeat when it comes again). True when it failed."""
    ev = pending(conn, task["id"])
    if ev is None:
        return False
    misses = [r for r in conn.execute("SELECT metadata FROM task_runs WHERE task_id=? AND"
                                      " profile='guide' AND ended_at>=?"
                                      " ORDER BY id", (task["id"], ev["created_at"]))
              if _ruling_of(r["metadata"]) in (ev["id"], None)]
    if len(misses) < MISSES:
        return False
    with txn(conn):
        event(conn, task["id"], "ruling_failed", {"contradiction": ev["id"],
                                                  "error": why[:500]}, now=now)
        failed(conn, cfg, task, ev, why, now)
    return True


def _ruling_of(metadata):
    """The `stall_ruling` event id a guide run ruled on (the tick writes `ruling_of`), else None. A
    run of the card's own review round has `review_of` and is not a ruling run: -1."""
    try:
        meta = json.loads(metadata or "{}")
        return -1 if meta.get("review_of") else meta.get("ruling_of")
    except (ValueError, AttributeError):
        return None


def failed(conn, cfg, task, ev, why, now):
    """Two invalid ruling documents: the ladder failed. No release; `stall._spent` pages at
    once; one `silent_failure` incident whose key has no card id, so a second one is a `repeat`."""
    cls = json.loads(ev["payload"])["class"]
    incidents.open_incident(conn, board=cfg.get("board") or "board", kind="silent_failure",
                            key=f"stall ladder failed: {cls}", task_id=task["id"],
                            evidence=[f"guide gave no valid stall ruling twice: {why}"[:500]],
                            now=now)


def _owner_record(conn, cfg, task, doc, now):
    """Open the owner-wait stall record at L2 with the ruling, `provisional` and `reversible`:
    `stall_owner` reads them. The next stall pass pages (`stall._spent`)."""
    key = f"stall:owner_wait:{task['id']}"
    rec = guard_get(conn, key)[0]
    hold = conn.execute(HOLD_Q, (task["id"],)).fetchone()
    since = hold[0] if hold else now
    if rec is None:
        it = {"class": "owner_wait", "subject": task["id"], "age_s": 0,
              "evidence": [f"hold: {hold[1] if hold else 'ruling'}"]}
        rec = {"since": since, "incident": stall_page.incident(conn, cfg, it, now), "paged_at": 0,
               "pages": 0, "slack_at": 0, "level": "L2", "evidence": it["evidence"]}
        event(conn, task["id"], "stall_opened", {"class": "owner_wait", "subject": task["id"],
                                                 "since": since, "incident": rec["incident"]},
              now=now)
    rec.update(ruling=doc["recommended"], provisional=doc.get("provisional"),
               reversible=bool(doc.get("reversible")))
    guard_set(conn, key, rec, now=now)


def _fault(conn, cfg, task, ev, doc, reason, now):
    """`release-capsule-fault`: the defect text joins the stall's incident (Korin's fix card is
    the Capsule fix) and one `file` line goes to the architect. No page."""
    cls = json.loads(ev["payload"])["class"]
    rec = guard_get(conn, f"stall:{cls}:{task['id']}")[0] or {}
    it = {"class": cls, "subject": task["id"], "age_s": 0, "evidence": ["hold: stall ruling"]}
    inc = rec.get("incident") or stall_page.incident(conn, cfg, it, now)
    if inc:
        incidents.attach(conn, inc, [f"guide: Capsule fault: {reason}"[:500]])
    outbox.add(conn, "file", f"capsule: stall ruling release-capsule-fault on {task['id']}:"
               f" {reason}"[:1500], now=now)


def apply(conn, cfg, run, doc, ev_id, now):
    """The guide's stall ruling. A release (`by='capsule'`) gives the card one more run and the
    note reaches the implementer as a guide ruling; `ask-owner` leaves the card held and opens the
    owner-wait record. A card that left the hold since the ruling began only records the event."""
    task_id, choice = run["task_id"], doc["recommended"]
    reason = doc.get("context") or doc["question"]
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    ev = conn.execute("SELECT * FROM task_events WHERE id=?", (ev_id,)).fetchone()
    waiting = task is not None and task["status"] in ("triage", "blocked")
    with txn(conn):
        done = False
        if waiting and choice in FAULT:
            if task["status"] == "triage":
                blocks.reset_failures(conn, task_id, by="capsule", now=now)
                done = True
            else:
                done = bool(blocks.unblock(conn, task_id, by="capsule", now=now))
        payload = {"choice": choice, "reason": reason, "contradiction": ev_id, "role": "stall",
                   "fault": FAULT.get(choice), "applied": done or (waiting and choice in ASKS),
                   "question": doc["question"][:300]}
        if choice in ASKS:
            payload.update(provisional=doc.get("provisional"),
                           reversible=bool(doc.get("reversible")))
        event(conn, task_id, "guide_ruling", payload, run_pk=run["id"], run_id=run["run_id"],
              now=now)
        if done and choice == "release-capsule-fault":
            _fault(conn, cfg, task, ev, doc, reason, now)
        elif waiting and choice in ASKS:
            _owner_record(conn, cfg, task, doc, now)
