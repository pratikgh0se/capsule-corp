"""Intervention outcome stamping (row 509 Zenkai Z7: a frozen file may
not grow, new logic goes into new modules)."""
import json
import re
import sys

from capsule import (blocks, config, dispatch, fix_card_drift, human, outbox, override,
                     patrol_check, preview, stall_continue, stall_owner, stall_remedies, stop_queue)
from capsule.db import event, guard_get, guard_set, txn


def stamp_outcome(conn, task_id, intervention_id, applied, refused_kind):
    """Adds `outcome`, `applied` or `refused:<reason>`, to the `intervention` event this
    file already got - all in the SAME `with txn(conn):` as the event insert and the
    verb call (`Tick._interventions`'s own wrapper; `blocks.*` nests as a SAVEPOINT),
    so a crash anywhere in between leaves nothing committed and the human file
    unarchived for a clean retry."""
    if applied:
        outcome = "applied"
    else:
        row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? AND id > ?"
                           " ORDER BY id DESC LIMIT 1", (task_id, refused_kind, intervention_id)).fetchone()
        reason = (json.loads(row[0]).get("reason") if row and row[0] else None) or "refused"
        outcome = f"refused:{reason}"
    conn.execute("UPDATE task_events SET payload=json_set(COALESCE(payload, '{}'), '$.outcome', ?) WHERE id=?",
                 (outcome, intervention_id))


DEFAULT_ASSIGNEE = "dev-backend"
HELD_BY_ANSWER = ("needs_input", "spec_stale")   # spec_stale: unblock refuses it, with its reason


def routing_gap(cfg, branch, assignee):
    """(branch, assignee, missing) a `reset` would leave on a card with this epic and assignee:
    the single declared epic and dev-backend fill a gap; `missing` names what stays unroutable."""
    declared = config.epic_branches(cfg)
    if branch is None and len(declared) == 1:
        branch = declared[0]
    assignee = DEFAULT_ASSIGNEE if assignee is None else assignee
    routable = bool(dispatch.resolve_assignee(cfg, assignee))
    missing = [n for n, ok in (("epic", branch in declared), ("assignee", routable)) if not ok]
    return branch, assignee, missing


def _route_reset(conn, cfg, task_id, now):
    """A card with no epic/assignee never dispatches: default them (single declared
    epic, dev-backend) or refuse loudly, never a silent stall. True when routable."""
    row = conn.execute("SELECT status, branch_name, assignee FROM tasks WHERE id=?",
                       (task_id,)).fetchone()
    if row is not None and row["status"] == "done":
        why = "the card is closed done and cannot be reset; unlanded work needs a new card"
        event(conn, task_id, "reset_refused", {"reason": why}, now=now)
        outbox.notify(conn, "blocked", f"{task_id} reset refused", fields=[("reason", why)],
                      thread_key=task_id, file_body=f"capsule: {task_id} reset refused: {why}",
                      now=now)
        return False
    if row is None or row["status"] not in ("triage", "blocked"):
        return True
    old = (row["branch_name"], row["assignee"])
    branch, assignee, missing = routing_gap(cfg, *old)
    if missing:
        why = (f"{' and '.join(missing)} missing or unroutable: run "
               "`epic <branch> [--assignee <name>]`, then `reset`")
        event(conn, task_id, "reset_refused", {"reason": why}, now=now)
        blocks._notify(conn, task_id, "reset refused", why, "epic", now)
        return False
    if (branch, assignee) != old:
        conn.execute("UPDATE tasks SET branch_name=?, assignee=? WHERE id=?",
                     (branch, assignee, task_id))
        event(conn, task_id, "epic_assigned",
              {"branch": branch, "assignee": assignee}, now=now)
    return True


def _unblock(conn, task_id, now, by="human"):
    """A release of a hold (`unblock`, or an `answer` on one); True when it lifted. `by` is
    `capsule` for the patrol's own entries (no runaway refill, no human verb)."""
    applied = blocks.unblock(conn, task_id, by=by, now=now)
    if applied:
        fix_card_drift.note_release(conn, task_id, now)
    return applied


_IDENT = re.compile(r"(?<![A-Za-z0-9_])_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")


def _shared_ids(conn, task_id, text):
    """Sorted contract identifiers (leading `_` stripped) the answer text shares with the body."""
    body = conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    found = [{t.lstrip("_") for t in _IDENT.findall(s or "")} for s in (text, body)]
    return sorted(found[0] & found[1])


def _answer(conn, cfg, task_id, doc, intervention_id, now):
    """Releases a needs_input hold like `unblock`, unless the answer names an identifier of the
    card body (inc_8b32f804: the ruling is not in the body yet, amend first); else a note."""
    row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task_id,)).fetchone()
    held = row["status"] == "blocked" and row["block_kind"] in HELD_BY_ANSWER
    shared = _shared_ids(conn, task_id, doc.get("text")) if held else []
    outcome = "amend_needed" if shared else "noted"
    if shared:
        outbox.notify(
            conn, "blocked", f"{task_id} answer kept the hold", thread_key=task_id,
            file_body=f"capsule: {task_id} answer kept the hold: it names "
            f"{', '.join(shared)} from the card body: amend the body (python3 -m "
            f"capsule human {cfg['board']} {task_id} amend --body-file <path> "
            "'<reason>'), then unblock", now=now)
    elif held:
        applied = _unblock(conn, task_id, now)
        return stamp_outcome(conn, task_id, intervention_id, applied, "unblock_refused")
    conn.execute("UPDATE task_events SET payload=json_set(payload, '$.outcome', ?) WHERE id=?",
                 (outcome, intervention_id))


CAPSULE_GATED = ("reset", "unblock", "continue", "rerun")   # the never list binds these


def _safe(conn, fn):
    """Run one verb of the patrol alone: a failure rolls back only that verb, never the file."""
    try:
        with txn(conn):
            return bool(fn())
    except Exception as exc:
        print(f"capsule tick: patrol verb failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def _rerun(conn, task_id, role, now):
    """The patrol's `rerun`: one re-run of a failed review role, on a `review_infra` hold only (the
    one automatic re-run of `stall_remedies`). Never a reset: it is `unblock` by capsule."""
    row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task_id,)).fetchone()
    return (row is not None and row["status"] == "blocked" and row["block_kind"] == "review_infra"
            and role in (None, *patrol_check.REVIEW_ROLES)    # a plain file names no role
            and not stall_owner.hold_reason(conn, task_id).startswith(stall_owner.KORIN)
            and not stall_remedies.infra_rerun_used(conn, task_id)
            and bool(blocks.unblock(conn, task_id, by="capsule", now=now)))


def _continue(conn, cfg, task_id, now):
    """The patrol's `continue`: one re-dispatch continue of a card, behind the gates of the quiet
    stop pass (status, safety, open run, pause, the day cap, a pending continue)."""
    card = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    run = conn.execute("SELECT * FROM task_runs WHERE task_id=? AND profile='implementer' AND"
                       " ended_at IS NOT NULL ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    cap = config.unattended(cfg)["continues_per_day"]
    if (card is None or run is None or dispatch.any_pause(conn, now)
            or stall_continue.pending(conn, task_id)
            or conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND id>?",
                            (task_id, run["id"])).fetchone()
            or stall_continue._counts(conn, now)[1] >= cap
            or not stall_continue._eligible(None, conn, card, run, None, now)):
        return False
    key = stall_continue._key(task_id)
    rec = guard_get(conn, key)[0] or {}
    made = stall_continue._continue(None, conn, cfg, card, run, {"kind": "patrol"}, rec,
                                    "redispatch", False, now)
    guard_set(conn, key, rec, now=now)
    return made


def _capsule_verb(conn, cfg, task_id, kind, doc, intervention_id, now):
    """An entry with `author: capsule` (the patrol). True when it is done here: refused by the
    never list again, or a `set-aside`, `continue` or `rerun` applied or refused. False: the
    normal verbs apply it, with `by=capsule`."""
    card = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if kind not in patrol_check.HUMAN_VERBS:    # never accept, close, override, archive, a screen
        return _refuse(conn, intervention_id, f"{kind} is not a patrol verb")
    barred = kind in CAPSULE_GATED and card is not None and stall_owner.never(
        conn, cfg, card, now)
    if kind in ("reset", "unblock", "rerun") and card is not None and (
            stall_owner.owner_held(conn, card)):
        barred = "the owner holds this card"
    if kind == "park" and not (card is not None and str(doc.get("text") or "").startswith(
            "duplicate of") and stall_owner.hold_reason(conn, task_id).startswith(
            stall_owner.KORIN)):
        barred = "park: only a Korin fix card, with text `duplicate of <card>: <why>`"
    if barred:
        return _refuse(conn, intervention_id, barred)
    verbs = {"set-aside": (lambda: stall_owner.set_aside(
                conn, task_id, now, why=str(doc.get("text") or "patrol")[:300]),
                "already set aside"),
             "continue": (lambda: _continue(conn, cfg, task_id, now), "not eligible"),
             "rerun": (lambda: _rerun(conn, task_id, doc.get("role"), now), "nothing to rerun")}
    if kind not in verbs:
        return False
    fn, why = verbs[kind]
    if _safe(conn, fn):
        conn.execute("UPDATE task_events SET payload=json_set(COALESCE(payload, '{}'),"
                     " '$.outcome', 'applied') WHERE id=?", (intervention_id,))
        return True
    return _refuse(conn, intervention_id, why)


def _refuse(conn, intervention_id, why):
    conn.execute("UPDATE task_events SET payload=json_set(COALESCE(payload, '{}'), '$.outcome',"
                 " ?) WHERE id=?", (f"refused:{why}", intervention_id))
    return True


def _reset(conn, cfg, task_id, by, now):
    """`reset`: route, zero the failures, lift. A queued stop is the owner's: only a human
    reset cancels it."""
    applied = _route_reset(conn, cfg, task_id, now)
    if applied:
        blocks.reset_failures(conn, task_id, by=by, now=now)
        fix_card_drift.note_release(conn, task_id, now)
        if by == "human":
            stop_queue.cancel(conn, task_id, "reset", now)
    return applied


def apply(conn, cfg, task_id, doc, src, file, now):
    """One human file's `intervention` event and its verb, in ONE `txn` (moved out of
    `Tick._interventions`, row 605: tick.py is frozen). `src` is the file's on-disk name (the
    idempotency key), `file` its archive name."""
    kind = doc.get("kind")
    with txn(conn):
        event(conn, task_id, "intervention",
              {"kind": kind, "text": doc.get("text"), "at": doc.get("at"),
               "file": file, "src": src,
               **({"tag": doc["tag"]} if doc.get("tag") else {}),
               **({"author": human.canonical_author(doc["author"])}
                  if doc.get("author") else {})}, now=now)
        intervention_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # `reset` fills epic/assignee or refuses, then zeroes failures + lifts blocked/triage;
        # `unblock` lifts blocked only. Capsule's own entries (`author: capsule`, the patrol)
        # act as `by=capsule`, pass the never list again here, and are no human verb.
        by = "capsule" if doc.get("author") == "capsule" else "human"
        if preview.handle(conn, task_id, kind, doc, intervention_id, now):
            return                # a preview card is the owner's: only `preview` moves it
        if by == "capsule" and _capsule_verb(conn, cfg, task_id, kind, doc, intervention_id, now):
            return
        if kind == "reset":
            applied = _reset(conn, cfg, task_id, by, now)
            stamp_outcome(conn, task_id, intervention_id, applied, "reset_refused")
        elif kind == "unblock":
            applied = _unblock(conn, task_id, now, by)
            stamp_outcome(conn, task_id, intervention_id, applied, "unblock_refused")
        elif kind == "answer":    # releases a needs_input hold like `unblock`; else a note
            _answer(conn, cfg, task_id, doc, intervention_id, now)
        elif kind == "accept":    # blocked + reported done -> review, no rerun
            applied = blocks.accept(conn, task_id, now=now)
            stamp_outcome(conn, task_id, intervention_id, applied, "accept_refused")
        elif kind in blocks.SCREEN_VERBS:   # row 369: the first screen's answer
            blocks.answer_first_screen(conn, task_id, doc["kind"], doc.get("text"), now=now)
        elif kind == "override":   # row 454 item 4: TTL'd per-check review bypass
            override.grant(conn, cfg, cfg["board"], task_id, check=doc.get("check"),
                           reason=doc.get("text"), ttl=doc.get("ttl"), fix_card=doc.get("fix_card"),
                           issuer=doc.get("issuer"), now=now)
        elif kind == "epic":   # reassign to a declared epic branch (+ Task 13 assignee)
            blocks.assign_epic(conn, task_id, doc.get("text"), declared=config.epic_branches(cfg),
                               assignee=doc.get("assignee"),
                               assignees=cfg.get("assignees"), now=now)
        elif kind == "park":   # hold on a named trigger, `reset` lifts it
            blocks.park(conn, task_id, doc.get("text"), now=now, by=by)
        elif kind == "stop":   # park once the current run ends; never signals the run
            applied = stop_queue.request(conn, task_id, doc.get("text"), now)
            stamp_outcome(conn, task_id, intervention_id, applied, "stop_refused")
        elif kind == "wait":   # hold on a declared epic; `reset` is the hand release
            declared = config.epic_branches(cfg)
            got = blocks.wait(conn, task_id, doc.get("text"), declared=declared, now=now)
            applied = got == "todo"
            stamp_outcome(conn, task_id, intervention_id, applied, "wait_refused")
        elif kind == "archive":   # never resumed: leaves its lane, nothing deleted
            applied = blocks.archive(conn, task_id, doc.get("text"), now=now)
            stamp_outcome(conn, task_id, intervention_id, applied, "archive_refused")
        elif kind == "close":   # landed outside the factory: done
            applied = blocks.close(conn, task_id, doc.get("text"), now=now)
            stamp_outcome(conn, task_id, intervention_id, applied, "close_refused")
