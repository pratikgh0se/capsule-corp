"""Block router and the failure gate (spec §6.4, rows 359, 376)."""
import json
import re
import time
from pathlib import Path

from capsule import db, outbox, slack_msgs
from capsule.db import CLEAR_CLAIM, event, open_parent, transition, txn
from capsule.lease import end_run
from capsule.runners import host_wall

# A run that ends with one of these classes pauses the board and is released, no failure counted
# (tick `_close`): a rate limit, and a host wall that cannot be applied (D161). A start-up check
# that timed out is released the same way but pauses nothing (`idoc.pause_scope`).
PAUSE_CLASSES = ("rate_limit", "auth_required", "account", host_wall.ERROR_CLASS,
                 host_wall.TIMEOUT_CLASS)
# review_infra (row 447): a review role gave no valid document twice; the
# implementer's work is not in question, so `accept` re-runs the review round on the
# same run.
VALID_BLOCK_KINDS = ("dependency", "needs_input", "capability", "transient", "review_infra",
                     "contradiction_loop", "spec_stale", "invalid_document", "epic_sync_conflict",
                     "epic_sync_error", "owner_look")
BLOCK_RECURRENCE_LIMIT = 2
RECHECK_WAITS = ("lander_wait_docker", "lander_wait_fetch", "lander_wait_env")   # lander.HOLDS
BLIND_CAP_LABEL = "blind cap (2 verifier rounds failed)"   # M110: blind.gate.cap's hold
APPROVAL_MAX_AGE = 7 * 86400   # an older completion is reviewed again on unblock, not landed
# The one list of event kinds that start and end a hold (inc_ac9dafef); a test scans the
# writers, so a new hold writer must name its event here.
HOLD_EVENTS = ("blocked", "failure_limit", "claims", "runaway", "contradiction",
               "contradiction_loop", "parked_by_human", "dependency_wait", "block_loop_detected")
LIFT_EVENTS = ("unblocked", "failure_reset")
# Every hold and park is one outbox line on the card's thread, in the status change's transaction.
NEEDS_INPUT_CLASS_KINDS = ("needs_input", "review_infra", "spec_stale", "invalid_document",
                          "epic_sync_conflict", "epic_sync_error")
# accept (row 447) lifts a triage card only when held by one of these kinds.
ACCEPTABLE_TRIAGE_KINDS = ("needs_input", "guide_ruling", "spec_stale", "review_infra")
# `epic`/`park` may only touch a card still in play.
REASSIGNABLE_STATUSES = ("todo", "ready", "scheduled", "blocked", "triage")
# `archive` also takes the legacy status the old dispatcher set (`replay.py:145`).
ARCHIVABLE_STATUSES = REASSIGNABLE_STATUSES + ("parked",)


def block(conn, task_id, *, kind, reason, run_pk=None, run_id=None, now=None, cause=None, offer=None):
    """`run_id` (the run's uuid) lands in the event payload, so a hold is tied to its
    run."""
    if kind not in VALID_BLOCK_KINDS:
        raise ValueError(f"unknown block kind {kind!r}")
    now = int(now or time.time())
    with txn(conn):
        row = conn.execute("SELECT block_kind, block_recurrences, current_run_id FROM tasks WHERE id=?",
                           (task_id,)).fetchone()
        if row is None:
            return None
        if kind == "dependency":
            route, cols = "todo", {"block_kind": "dependency"}
            kind_event, payload = "dependency_wait", {"reason": reason, "kind": kind}
        else:
            recur = row["block_recurrences"] + 1 if row["block_kind"] == kind else 1
            route = "triage" if recur >= BLOCK_RECURRENCE_LIMIT else "blocked"
            cols = {"block_kind": kind, "block_recurrences": recur}
            kind_event = "block_loop_detected" if route == "triage" else "blocked"
            payload = {"reason": reason, "kind": kind, "recurrences": recur}
        if cause:
            payload["cause"] = cause
        where, params = (" AND current_run_id=?", (run_pk,)) if run_pk is not None else ("", ())
        if not transition(conn, task_id, route, frm=("running", "ready", "review"), claim=True,
                          where=where, params=params, **cols):
            return None
        if row["current_run_id"] is not None:
            end_run(conn, row["current_run_id"], status="blocked", outcome="blocked", summary=reason, now=now)
        event(conn, task_id, kind_event, payload, run_pk=row["current_run_id"] or _run_pk(conn, run_id),
              run_id=run_id, now=now)
        if kind != "dependency":
            verbs = (offer if offer is not None and route == "blocked" else
                     "accept|reset" if kind in ("review_infra", "spec_stale")
                     else "reset|accept" if kind == "needs_input" and route != "blocked"
                     else "reset" if route != "blocked"
                     else "unblock|reset|accept" if kind == "needs_input" else "unblock|reset")
            cls = "needs_input" if kind in NEEDS_INPUT_CLASS_KINDS else "blocked"
            shown = blind_cap_label(cause) or kind
            _notify(conn, task_id, f"{route} ({shown})", reason, verbs, now, cls=cls,
                    key=slack_msgs.needs_key(task_id))
    return route


def _run_pk(conn, run_id):
    """`task_runs.id` of the run named by `run_id`, for the event's join column."""
    if run_id is None:
        return None
    row = conn.execute("SELECT id FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
    return row[0] if row else None


def _board_name(conn):
    """`boards/<board>/kanban.db` -> `<board>`, for a reply line the owner can paste."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2]).parent.name
    return "<board>"


def _notify(conn, task_id, what, reason, verbs, now, *, cls="blocked", key=None):
    reply = f"python3 -m capsule human {_board_name(conn)} {task_id} {verbs} '<answer>'"
    body = f"capsule: {task_id} {what}: {' '.join(reason[:1500].split())} | reply: {reply}"
    outbox.notify(conn, cls, f"{task_id} {what}", fields=[("reason", reason)], reply=reply,
                  thread_key=task_id, file_body=body, key=key, now=now)


def _handled(conn, task_id, now):
    """Every release of a held card edits its 🙋 message to `✔ {Name} #{n}: handled.`, whoever
    released it (human verb, worker unblock, guide reset or accept): a later ask posts afresh.
    The release also closes the card's decision request: a later block for another cause is not
    an owner wait."""
    from capsule import statusview
    conn.execute("UPDATE tasks SET request_needs=NULL WHERE id=? AND request_needs='decision'",
                 (task_id,))
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    number = statusview._number(row)
    tag = f" #{number}" if number else ""
    line = f"{slack_msgs.HANDLED} {statusview._name(row)}{tag}: handled."
    outbox.finish(conn, slack_msgs.needs_key(task_id), line, now)


def assign_epic(conn, task_id, branch, *, declared, assignee=None, assignees=None, now=None):
    """`capsule human <board> <task> epic <branch>` (row 456):
    sets `tasks.branch_name` to `branch`, one of the epic branches declared under
    config `epics` (`config.epic_branches(cfg)` / `epic_branches_from_list`, passed
    in as `declared` - blocks.py itself never reads config, same seam
    `promote_ready`'s own `branches` param already uses)."""
    now = int(now or time.time())
    with txn(conn):
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            why = "no such task"
        elif row["status"] not in REASSIGNABLE_STATUSES:
            why = f"{row['status']}: cannot reassign"
        elif branch not in declared:
            why = f"{branch!r} is not declared under config epics"
        elif assignee is not None and (not isinstance(assignee, str)
                                       or (assignees or {}).get(assignee) != "implementer"):
            why = f"assignee {assignee} does not route to the implementer"
        else:
            why = None
        if why:
            event(conn, task_id, "epic_refused", {"branch": branch, "reason": why}, now=now)
            _notify(conn, task_id, "epic refused", f"{branch!r}: {why}", "epic <declared-branch>", now)
            return False
        conn.execute("UPDATE tasks SET branch_name=?, assignee=COALESCE(?, assignee) WHERE id=?",
                     (branch, assignee, task_id))
        extra = {} if assignee is None else {"assignee": assignee}
        event(conn, task_id, "epic_assigned", {"branch": branch, **extra}, now=now)
    return True


def park(conn, task_id, trigger, *, now=None, queued=False, by="human"):
    """`capsule human <board> <task> park '<trigger text>'`: holds the card `blocked` with
    `block_kind='parked'` until `reset` lifts it; `unblock` refuses it."""
    now = int(now or time.time())
    with txn(conn):
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            why = "no such task"
        elif not isinstance(trigger, str) or not trigger.strip():
            why = "park needs a non-empty trigger"
        elif row["status"] not in REASSIGNABLE_STATUSES + (("running", "review") if queued else ()):
            why = f"{row['status']}: cannot park"
        else:
            why = None
        if why:
            event(conn, task_id, "park_refused", {"trigger": trigger, "reason": why}, now=now)
            _notify(conn, task_id, "park refused", why, "park '<trigger>'", now)
            return False
        transition(conn, task_id, "blocked", claim=True, block_kind="parked", waits_on_branch=None)
        payload = {"by": by, "trigger": trigger, **({"queued": True} if queued else {})}
        if row["block_kind"] is not None:
            payload["was"] = row["block_kind"]
        event(conn, task_id, "parked_by_human", payload, now=now)
        _notify(conn, task_id, "parked", trigger, "reset", now)
    return True


WAIT_SHA = re.compile(r"[0-9a-f]{7,40}")   # `wait` takes a commit sha beside a declared epic
WAITABLE_STATUSES = ("ready", "todo", "blocked")
WAIT_REFUSED_KINDS = ("parked", "spec_stale")


def _blind_held(conn, task_id):
    """True when the card's newest hold carries a blind-gate cause."""
    from .blind import gate
    r = conn.execute("SELECT json_extract(payload,'$.cause') FROM task_events WHERE task_id=?"
                     " AND kind IN ('blocked','block_loop_detected') ORDER BY id DESC LIMIT 1",
                     (task_id,)).fetchone()
    return bool(r and str(r[0] or "").startswith(f"{gate.CAUSE}:"))


def wait(conn, task_id, branch, *, declared, now=None):
    """`capsule human <board> <task> wait <epic-branch>`: holds the card `todo` with
    `block_kind='dependency'` until a human `reset` lifts it; a 7-40 hex sha (not an epic)
    also lifts when the tick sees it in main (`main_waits.release`). Accepts ready,
    todo and blocked (not parked or spec_stale); refuses every other status. Returns 'todo', or
    None after a refusal that changed nothing."""
    now = int(now or time.time())
    with txn(conn):
        row = conn.execute("SELECT status, block_kind, branch_name FROM tasks WHERE id=?",
                           (task_id,)).fetchone()
        why = None
        if row is None:
            why = "no such task"
        elif branch not in declared and not (isinstance(branch, str)
                                             and WAIT_SHA.fullmatch(branch)):
            why = f"{branch}: not declared"
        elif branch == row["branch_name"]:
            why = "own branch"
        elif row["status"] not in WAITABLE_STATUSES:
            why = f"{row['status']}: cannot wait"
        elif row["status"] == "blocked" and row["block_kind"] in WAIT_REFUSED_KINDS:
            why = f"{row['block_kind']}: reset/accept first"
        elif row["status"] == "blocked" and _blind_held(conn, task_id):
            why = "blind_gate: blind hold, reset/accept first"
        if why:
            event(conn, task_id, "wait_refused", {"branch": branch, "reason": why}, now=now)
            _notify(conn, task_id, "wait refused", why, "wait <declared-branch>", now)
            return None
        transition(conn, task_id, "todo", claim=True, block_kind="dependency",
                   waits_on_branch=branch)
        if row["status"] == "blocked":
            event(conn, task_id, "unblocked", {"status": "todo"}, now=now)
            _handled(conn, task_id, now)
        event(conn, task_id, "dependency_wait",
              {"reason": f"waits on {branch}; reset lifts it", "kind": "dependency",
               "waits_on": branch},
              now=now)
    return "todo"


def close(conn, task_id, reason, *, now=None):
    """`capsule human <board> <task> close '<why>'`: the work landed outside the
    factory, so the card is `done` and `open_parent` stops returning it;
    `promote_ready` (every tick) then moves its todo children to ready."""
    now = int(now or time.time())
    with txn(conn):
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            why = "no such task"
        elif not isinstance(reason, str) or not reason.strip():
            why = "close needs a non-empty reason"
        elif row["status"] not in REASSIGNABLE_STATUSES:
            why = f"{row['status']}: cannot close"
        else:
            why = None
        if why:
            event(conn, task_id, "close_refused", {"reason": why}, now=now)
            _notify(conn, task_id, "close refused", why, "close '<why>'", now)
            return False
        transition(conn, task_id, "done", claim=True, block_kind=None, completed_at=now)
        event(conn, task_id, "closed",
              {"by": "human", "reason": reason, "was": row["status"]}, now=now)
        _handled(conn, task_id, now)
    return True


def archive_blocker(conn, task_id):
    """Why archiving the card would strand other work, else None: unfinished children (an archived
    parent counts as settled, so they would be promoted without its work) or the fix card of an
    open incident (its close gate needs that card done)."""
    kids = [r[0] for r in conn.execute(
        "SELECT c.id FROM task_links l JOIN tasks c ON c.id=l.child_id WHERE l.parent_id=?"
        " AND c.status NOT IN ('done','archived') ORDER BY c.id", (task_id,))]
    if kids:
        return f"has unfinished children: {', '.join(kids)}"
    inc = conn.execute("SELECT id FROM incidents WHERE fix_card=? AND status NOT IN"
                       " ('closed','superseded') ORDER BY id LIMIT 1", (task_id,)).fetchone()
    return f"fix card of open incident {inc[0]}" if inc else None


def archive(conn, task_id, reason, *, now=None):
    """`capsule human <board> <task> archive '<why>'`: a card that will never be resumed leaves its
    lane (`epic_clean.holders` skips `archived`). Refused for a missing task, a blank reason, a
    live claim, any status outside `ARCHIVABLE_STATUSES` (running, review, done; the legacy
    `parked` is accepted), unfinished children, or being an open incident's fix card
    (`archive_blocker`). Deletes
    nothing: the clone is `epic_clean`'s to clean once nothing holds it."""
    now = int(now or time.time())
    with txn(conn):
        row = conn.execute("SELECT status, claim_lock FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            why = "no such task"
        elif not isinstance(reason, str) or not reason.strip():
            why = "archive needs a non-empty reason"
        elif row["claim_lock"] is not None:
            why = "a live claim holds it"
        elif row["status"] not in ARCHIVABLE_STATUSES:
            why = f"{row['status']}: cannot archive"
        else:
            why = archive_blocker(conn, task_id)
        if why:
            event(conn, task_id, "archive_refused", {"reason": why}, now=now)
            _notify(conn, task_id, "archive refused", why, "archive '<why>'", now)
            return False
        transition(conn, task_id, "archived", claim=True, block_kind=None)
        event(conn, task_id, "archived",
              {"by": "human", "reason": reason, "was": row["status"]}, now=now)
        _handled(conn, task_id, now)
    return True


def blind_cap_label(cause):
    """The status-view label for a hold whose hold cause is `blind_cap:<digest>`, else None."""
    return BLIND_CAP_LABEL if str(cause or "").startswith("blind_cap:") else None


def cap_hold_label(conn, task_id):
    """`blind_cap_label` of the card's newest hold event (`blocked`, or `block_loop_detected` on
    a repeat), so an older hold's cause never speaks for a later one."""
    last = conn.execute("SELECT json_extract(payload, '$.cause') FROM task_events WHERE task_id=?"
                        " AND kind IN ('blocked', 'block_loop_detected') ORDER BY id DESC LIMIT 1",
                        (task_id,)).fetchone()
    return blind_cap_label(last and last[0])


def lander_wait(conn, task_id, before=2**62):
    """The `lander_wait_*` cause of the card's newest hold or lift event before event id
    `before`, when that event is a `blocked` that `lander.record` wrote (its structured `cause`,
    which no worker verb can set) and a second landing check can settle: the sandbox or origin
    was unavailable, or a gate kept refusing for the environment. Not `lander_wait_rebase`: a
    conflict needs rework. Else None: a later hold (a failure limit, a contradiction loop) or a
    lift outranks an older wait."""
    kinds = ",".join(f"'{k}'" for k in HOLD_EVENTS + LIFT_EVENTS)
    row = conn.execute("SELECT kind, json_extract(payload, '$.cause') FROM task_events WHERE "
                       f"task_id=? AND id<? AND kind IN ({kinds}) ORDER BY id DESC LIMIT 1",
                       (task_id, before)).fetchone()
    return row[1] if row and row[0] == "blocked" and row[1] in RECHECK_WAITS else None


def _recheck_wait(conn, task_id, now):
    """True when unblocking the card sends it back to `done` for a second landing check: its
    hold is a `lander_wait` and the approval still stands, which means no `close` and no
    `contract_amended` after the latest `completed` event (the text the reviewers approved
    changed) and that completion is at most APPROVAL_MAX_AGE old. Otherwise it is `ready`."""
    done = conn.execute("SELECT id, created_at FROM task_events WHERE task_id=? AND "
                        "kind='completed' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if done is None or not lander_wait(conn, task_id) or was_closed(conn, task_id):
        return False
    amended = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND "
                           "kind='contract_amended' AND id>?", (task_id, done[0])).fetchone()
    return amended is None and now - done[1] <= APPROVAL_MAX_AGE


def not_closed_after(task_col, event_col):
    """SQL condition: no person's `closed` event on the task came after the `completed` event, so
    a card closed by hand never counts as landed with that event's commit (`was_closed` for one
    card). Both arguments are column names, `t.id` and `e.id`."""
    return (f"NOT EXISTS (SELECT 1 FROM task_events cl WHERE cl.task_id={task_col} AND "
            f"cl.kind='closed' AND cl.id>{event_col})")


def was_closed(conn, task_id):
    """True when a person's `close` came after the card's latest `completed` event: its work
    landed outside the factory, so the lander must not check, land or block it. A `reset`
    leaves a closed card `done` (`reset_failures` moves only triage and blocked cards); a card
    completed again by another route is an ordinary one. The lander still records and verifies
    a push it already made for a closed card: that guards main whatever the card's state."""
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='closed' AND id>"
        "(SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=? AND kind='completed')",
        (task_id, task_id)).fetchone() is not None


def _worker_refused(conn, task_id, row, by, now):
    """A worker never releases a decision card the owner must answer (the veto revert card)."""
    if by != "worker" or row["status"] != "blocked" or row["request_needs"] != "decision":
        return False
    event(conn, task_id, "unblock_refused", {"reason": "decision: the owner answers"}, now=now)
    return True


def unblock(conn, task_id, *, by, now=None):
    """Row 472: a `spec_stale` block is `accept|reset` only - unblock is refused (never
    a rerun of the implementer): the card's work is not what's wrong, the spec is,
    and only the owner editing the spec section (then `accept`) or `reset` (a fresh
    round) makes sense here. A lander wait (`_recheck_wait`) goes back to `done`, to be
    landing-checked again on the same commit: no implementer run, no review round. Not when
    the approval is stale (see `_recheck_wait`): that card goes to `ready`."""
    if by not in ("human", "worker", "capsule"):
        raise ValueError(f"unblock: by must be 'human', 'worker' or 'capsule', got {by!r}")
    now = int(now or time.time())
    with txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or _worker_refused(conn, task_id, row, by, now):
            return False
        if row["status"] == "blocked" and row["block_kind"] == "spec_stale":
            event(conn, task_id, "unblock_refused", {"reason": "spec_stale: accept|reset only"}, now=now)
            if by == "human":
                _notify(conn, task_id, "unblock refused", "spec_stale: accept|reset only, the spec"
                       " edit is the owner's to make", "accept|reset", now)
            return False
        # a human `park` lifts on `reset` only; a worker gets the event, no notice.
        if row["status"] == "blocked" and row["block_kind"] == "parked":
            event(conn, task_id, "unblock_refused", {"reason": "parked: reset only"}, now=now)
            if by == "human":
                _notify(conn, task_id, "unblock refused", "parked: reset only, a park lifts on reset",
                       "reset", now)
            return False
        if row["status"] == "triage":
            verbs = "reset|accept" if row["block_kind"] in ACCEPTABLE_TRIAGE_KINDS else "reset"
            event(conn, task_id, "unblock_refused", {"reason": f"triage: {verbs} only"}, now=now)
            if by == "human":
                tail = " or the owner's accept" if verbs != "reset" else ""
                _notify(conn, task_id, "unblock refused", f"triage: {verbs} only, the block loop"
                       f" needs a fresh round{tail}", verbs, now)
            return False
        back = row["status"] == "blocked" and _recheck_wait(conn, task_id, now)
        run = _conflict_run(conn, task_id, row, by)
        landing = ("done" if back else "todo" if open_parent(conn, task_id) is not None
                   else "review" if run else "ready")
        clear = {"block_kind": None, "block_recurrences": 0} if by == "human" else {}   # row 511
        if back or (by == "capsule" and landing in ("review", "ready")):
            clear["block_kind"] = None      # a done, review or ready card holds nothing
        frm = ("blocked",) if run else ("blocked", "scheduled")
        if not transition(conn, task_id, landing, frm=frm, current_run_id=None,
                          consecutive_failures=0, last_failure_error=None, **clear):
            kept = by == "human" and row["status"] not in ("done", "archived")
            why = f"{row['status']}: cannot unblock" + (", kept as an answer" if kept else "")
            event(conn, task_id, "unblock_refused", {"reason": why}, now=now)
            if by == "human":
                _notify(conn, task_id, "unblock refused", why, "reset", now)
            return False
        event(conn, task_id, "unblocked",
              {"by": by, **({"status": landing} if landing != "ready" else {})}, now=now)
        if landing == "review":     # as `accept`: the review round reruns on the merged clone
            after = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?",
                                 (task_id,)).fetchone()[0]
            event(conn, task_id, "request_review", {"by": "unblock", "after_run": after},
                  run_pk=run["id"], run_id=run["run_id"], now=now)
        _handled(conn, task_id, now)
    return True


def _conflict_run(conn, task_id, row, by):
    """inc_2fd5958b: the card's latest implementer run when a human `unblock` lifts an
    `epic_sync_conflict` hold (gates passed, work committed; a hand merge fixes the clone), else
    None. The card goes to review on that run, never back to an implementer."""
    redo = by == "capsule" and row["block_kind"] == "review_infra"     # stall L0: re-run the role
    if row["status"] != "blocked" or not (redo or (
            by == "human" and row["block_kind"] == "epic_sync_conflict")):
        return None
    return conn.execute("SELECT id, run_id FROM task_runs WHERE task_id=? AND profile='implementer'"
                        " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()


def accept(conn, task_id, *, now=None):
    """The owner accepts a held card's work as reported."""
    now = int(now or time.time())
    with txn(conn):
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task_id,)).fetchone()
        run = conn.execute("SELECT id, run_id, handoff FROM task_runs WHERE task_id=? AND profile='implementer'"
                           " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        try:
            report = json.loads(run["handoff"]) if run is not None and run["handoff"] else None
        except ValueError:
            report = None
        # a guide_ruling triage card (row 454: an intent contradiction routed to the guide) too;
        # spec_stale/review_infra: their notice is accept|reset even at recurrence 2
        looped = row is not None and row["status"] == "triage" and row["block_kind"] in ACCEPTABLE_TRIAGE_KINDS
        # a human `park` is `reset`-only, as for `unblock`, whatever the last run reported.
        parked = row is not None and row["status"] == "blocked" and row["block_kind"] == "parked"
        if parked:
            why = "parked: reset only"
        elif row is None or (row["status"] != "blocked" and not looped):
            why = f"the card is {row['status'] if row else 'missing'}, not blocked (or in triage on needs_input/guide_ruling)"
        # a block-loop triage run reported the hold, not done; any implementer run is enough to
        # review.
        elif row["status"] == "blocked" and (not isinstance(report, dict) or report.get("status") != "done"):
            why = "its last implementer run did not report status: done"
        elif looped and run is None:
            why = "no implementer run to review"
        else:
            why = None
        if why:
            event(conn, task_id, "accept_refused", {"reason": why}, now=now)
            # a parked card's `unblock` is refused too, so offer `reset` only.
            _notify(conn, task_id, "accept refused", why, "reset" if parked else "unblock|reset", now)
            return False
        # spec_stale/review_infra keep counting - a second hold there is still the same
        # unresolved loop; every other kind is the owner's answer, so the next hold starts fresh.
        keep = row["block_kind"] in ("spec_stale", "review_infra")
        transition(conn, task_id, "review", claim=True, current_run_id=None,
                   **({} if keep else {"block_recurrences": 0}))
        # The review stage takes the latest implementer run that completed or was
        # accepted (a `request_review` event names it), so the accepted run is the one
        # reviewed; its round starts after every run so far (`after_run`), so a re-
        # accepted run is reviewed afresh (row 447).
        after = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
        event(conn, task_id, "request_review", {"by": "accept", "after_run": after}, run_pk=run["id"],
              run_id=run["run_id"], now=now)
        _handled(conn, task_id, now)
    return True


def record_failure(conn, task_id, *, reason, limit, back_to="ready", run_id=None, now=None,
                   git_lock=False):
    """Count one failed run; at the limit the card goes to triage (`failure_limit`). The only
    failure gate. `run_id` (the failed run's uuid) lands in the event payload.

    `git_lock`: the run's error names the read-only git dir. The mount is correct by design, so
    it is one `implementer_note` (not a failure) and the card goes back to ready for a retry that
    follows the contract line; after GIT_LOCK_RETRY_LIMIT such notes (or when the card is no
    longer running) it counts as usual. A fatal git-lock run still emits `crashed` (a Korin
    incident) before reaching here; that is kept on purpose."""
    now = int(now or time.time())
    prior = GIT_LOCK_RETRY_LIMIT
    if git_lock:
        prior = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='implementer_note'"
            " AND json_extract(payload,'$.kind')='git_dir_read_only_failed'",
            (task_id,)).fetchone()[0]
    if git_lock and prior < GIT_LOCK_RETRY_LIMIT:
        with txn(conn):
            if transition(conn, task_id, back_to, frm=("running",), claim=True):
                event(conn, task_id, "implementer_note",
                      {"kind": "git_dir_read_only_failed", "count": 1},
                      run_pk=_run_pk(conn, run_id), run_id=run_id, now=now)
                return back_to
    with txn(conn):
        cur = conn.execute("UPDATE tasks SET failure_count = failure_count + 1, last_failure_error=? WHERE id=?",
                           (reason[:2000], task_id))
        if cur.rowcount != 1:
            return None
        count, current = conn.execute("SELECT failure_count, status FROM tasks WHERE id=?", (task_id,)).fetchone()
        new = "triage" if count >= limit else back_to
        transition(conn, task_id, new, claim=True)
        kind_event = "failure_limit" if new == "triage" and current != "triage" else "failed"
        event(conn, task_id, kind_event, {"reason": reason[:500], "failure_count": count, "limit": limit},
              run_pk=_run_pk(conn, run_id), run_id=run_id, now=now)
        if kind_event == "failure_limit":
            _notify(conn, task_id, f"triage (failure {count}/{limit})", reason, "reset", now)
    return new


_GIT_WORD_RE = re.compile(r"\bgit\b", re.IGNORECASE)
_GIT_LOCK_MARKER_RE = re.compile(
    r"index\.lock|\.git\b[^.;\n]{0,80}\b(read[- ]only|erofs)"
    r"|\b(read[- ]only|erofs)\b[^.;\n]{0,80}\.git\b",
    re.IGNORECASE)
GIT_LOCK_RETRY_LIMIT = 2


_CLAUSE_SPLIT_RE = re.compile(r"[.;]\s+|\n")


# The only wordings a clause may have without naming the lock itself (anchored at its start).
_HARMLESS_CLAUSE_RE = re.compile(
    r"^\W*(?:retried|retrying|worked around|the work is (?:complete|committed)"
    r"|committed nothing)\b",
    re.IGNORECASE)


def mentions_git_lock(text):
    """`git` plus a lock/read-only marker anywhere in `text`. Right for a failed run's raw
    error (git's own stderr), where the whole text is the tool's, not a worker's report."""
    t = text or ""
    return bool(_GIT_WORD_RE.search(t)) and bool(_GIT_LOCK_MARKER_RE.search(t))


def is_git_lock_note(text):
    """A git-lock note for a worker's own `left_undone`/`issues` item: it mentions the lock AND
    every clause either names the lock itself (git + `index.lock`, or `.git` read-only) or is one
    of a few known harmless wordings ("retried", "worked around", "the work is complete").
    Anything else is a real gap glued on ("... still needs a test; git commit failed:
    index.lock") and the item is not a note, so it still holds."""
    if not mentions_git_lock(text):
        return False
    return all(_GIT_LOCK_MARKER_RE.search(c) or _HARMLESS_CLAUSE_RE.match(c)
               for c in _CLAUSE_SPLIT_RE.split(text) if c.strip())


_BODY_ITEM_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?(?:(?:item|step|point)\s+)?#?([A-Za-z0-9]+)[.):]\s+(.*)$",
    re.IGNORECASE)


_OPTIONAL_MARK_RE = re.compile(r"^(?:\(optional\)|optional:|optional -)", re.IGNORECASE)
# The closed list: three forms, one final period allowed. A wording outside it holds by design.
_OPTIONAL_NOTE_RE = re.compile(
    r"(?:item\s+(?P<a>[A-Za-z0-9]+)(?:\s+\(optional\s[^()]+\))?\s+not done"
    r"|optional\s+item\s+(?P<b>[A-Za-z0-9]+)\s+not done)\.?",
    re.IGNORECASE)


def _optional_items(card_body):
    """Labels the card marks `optional` at the start of the item ("4. (optional) fold modules").
    A label that any other item also uses without the marker is ambiguous and left out."""
    optional, required = set(), set()
    for line in (card_body or "").splitlines():
        m = _BODY_ITEM_RE.match(line)
        if m:
            marked = _OPTIONAL_MARK_RE.match(m.group(2).strip())
            (optional if marked else required).add(m.group(1).lower())
    return optional - required


def is_optional_item_note(text, card_body):
    """True when `text` is one of the three closed forms and the card body marks that item
    optional. Any other line holds by design."""
    m = _OPTIONAL_NOTE_RE.fullmatch(text.strip())
    return bool(m) and (m.group("a") or m.group("b")).lower() in _optional_items(card_body)


def note_optional_items(conn, task_id, report, run, now, card_body):
    """One informational `implementer_note` event (kind `optional_item_skipped`) when
    `left_undone` skips an optional card item: never a block, never a Korin incident."""
    hits = [x for x in report["left_undone"] if is_optional_item_note(x, card_body)]
    if hits:
        with txn(conn):
            event(conn, task_id, "implementer_note",
                  {"kind": "optional_item_skipped", "count": len(hits)},
                  run_pk=run["id"], run_id=run["run_id"], now=now)


def drop_optional_item_notes(report, card_body):
    """A copy of `report` without the `left_undone` lines that skip an optional card item."""
    return dict(report, left_undone=[x for x in report["left_undone"]
                                     if not is_optional_item_note(x, card_body)])


def reset_failures(conn, task_id, *, by, now=None):
    """Zero the counter and release the card in one transaction (owner, or approved reset)."""
    now = int(now or time.time())
    with txn(conn):
        landing = "todo" if open_parent(conn, task_id) is not None else "ready"
        conn.execute("UPDATE tasks SET failure_count=0, failure_reset_at=?, block_kind=NULL,"
                     " block_recurrences=0, waits_on_branch=NULL WHERE id=?", (now, task_id))
        transition(conn, task_id, landing, frm=("triage", "blocked"))
        event(conn, task_id, "failure_reset", {"by": by}, now=now)
        _handled(conn, task_id, now)


def promote_ready(conn, *, branches=None, now=None):
    """`todo` -> `ready` for every task whose parents are all done or archived (a
    child was never promoted when its parent finished, so an epic stopped after its
    first card)."""
    now = int(now or time.time())
    promoted = []
    sql, params = "SELECT id FROM tasks WHERE status='todo' AND waits_on_branch IS NULL", ()
    if branches is not None:
        branches = tuple(branches)
        if not branches:
            return promoted
        sql += f" AND branch_name IN ({','.join('?' * len(branches))})"
        params = branches
    with txn(conn):
        for (task_id,) in conn.execute(sql + " ORDER BY created_at", params).fetchall():
            if promote_one(conn, task_id, now=now):
                promoted.append(task_id)
    return promoted


def promote_one(conn, task_id, *, now=None):
    """`todo` -> `ready` for one card with no open parent (M137: an image request's waiter, whatever
    its branch). True when it moved."""
    if open_parent(conn, task_id) is not None:
        return False
    if not transition(conn, task_id, "ready", frm=("todo",)):
        return False
    parents = [r[0] for r in conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id",
        (task_id,)).fetchall()]
    event(conn, task_id, "promoted", {"parents": parents}, now=int(now or time.time()))
    return True


# -- first screen to the owner (spec §5, §6.7; row 369)
# --------------------------------------- One guard_state key per UI epic: absent until
# its first screen passes review; {"state": "held"} holds the whole epic; "fix" lets
# only the returned card dispatch; "ok" is released for good.
FIRST_SCREEN_KEY = "first_screen:{}"
SCREEN_VERBS = ("screen-ok", "screen-fix")


def first_screen(conn, branch):
    value, _ = db.guard_get(conn, FIRST_SCREEN_KEY.format(branch))
    return value if isinstance(value, dict) else None


def hold_first_screen(conn, task_id, branch, *, screens, commit, now=None):
    """The card's review passed and it rendered the epic's first screen: hold the epic and send it
    to the owner with the reply line (the tick's outbox is the chief of staff's channel, §6.5)."""
    now = int(now or time.time())
    with txn(conn):
        db.guard_set(conn, FIRST_SCREEN_KEY.format(branch), {"task_id": task_id, "state": "held"}, now=now)
        event(conn, task_id, "first_screen_hold", {"branch": branch, "screens": screens, "commit": commit}, now=now)
        shown = ", ".join(screens) or "(no screen paths declared: the card's whole change)"
        body = (f"capsule: {task_id} first screen of {branch} (row 369), epic held until you answer:"
                f" {shown} at commit {commit or 'n/a'} |"
                f' reply: python3 -m capsule human {_board_name(conn)} {task_id} screen-ok|screen-fix "<note>"')
        outbox.notify(conn, "blocked", f"{task_id} first screen of {branch}: epic held until you answer",
                      fields=[("screens", shown), ("commit", commit or "n/a")],
                      reply=f'python3 -m capsule human {_board_name(conn)} {task_id} screen-ok|screen-fix "<note>"',
                      thread_key=task_id, file_body=body,
                      key=slack_msgs.needs_key(task_id), now=now)


def answer_first_screen(conn, task_id, kind, note, *, now=None, by=None):
    """`screen-ok` releases the held epic; `screen-fix` returns the card (done -> ready, the note
    reaches its rerun as a human answer) and only that card may dispatch until it passes again."""
    now = int(now or time.time())
    with txn(conn):
        row = conn.execute("SELECT branch_name, status FROM tasks WHERE id=?", (task_id,)).fetchone()
        held = first_screen(conn, row["branch_name"]) if row is not None else None
        if held is None or held.get("task_id") != task_id or held.get("state") != "held":
            why = "no first screen of this card is waiting for an answer"
            event(conn, task_id, "screen_refused", {"kind": kind, "reason": why}, now=now)
            outbox.notify(conn, "running", f"{task_id} {kind} refused: {why}",
                          file_body=(f"capsule: {task_id} {kind} refused: {why}; nothing to do,"
                                     " reply once its first-screen line arrives"), thread_key=task_id, now=now)
            return False
        state = "ok" if kind == "screen-ok" else "fix"
        db.guard_set(conn, FIRST_SCREEN_KEY.format(row["branch_name"]), dict(held, state=state), now=now)
        if state == "fix":
            landing = "todo" if open_parent(conn, task_id) is not None else "ready"
            transition(conn, task_id, landing, frm=("done",), completed_at=None)
        event(conn, task_id, "first_screen_" + state,
              {"note": note, **({"by": by} if by else {})}, now=now)
        _handled(conn, task_id, now)
    return True
