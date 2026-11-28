"""Quiet stops: continue is the default (docs/designs/stall-supervisor-unattended.md 9.10).

An implementer run that ends with nothing decided (no diff, a question, far too early, the same
state) or with a step the harness itself performs (`untrack`) is not a stall to page about: the
card continues by itself. `on_close` records one `run_closed` event per implementer run with its
progress mark; `step` (called by `stall.step`) turns each quiet one into a new run:

  1. resume: the same model session (`claude -p --resume`), when the run gave a session id;
  2. re-dispatch: a fresh run, the missing text as a `guide_ruling` note (`by` capsule);
  3. the stronger tier of the role's route (`redesign.escalation`), once per 24 h;
  4. set aside: a `guard_state` `aside:<card>` row and one incident. The owner is not paged.

Caps: 2 resume or re-dispatch continues per card and 30 per board in 24 h
(`unattended.continues_per_day`). A run that moves the branch head starts the count again. A
continue is no failed review round: it adds no `failed` event, so `runaway` counts nothing.
`step` only prepares the card (back to `ready`, a plan in `guard_state`); the normal dispatcher
starts the run, so every pause and lane rule still applies.
"""
import hashlib
import re
import sys
from pathlib import Path

from capsule import blocks, config, incidents, restart, stall_page, workspace
from capsule.db import event, guard_get, guard_set, transition, txn
from capsule.stall_continue_text import (  # noqa: F401
    HARNESS_TEXT, NOBODY, UNCHECKED, _closed, _items, _json, missing)

KINDS = ("run_empty", "run_asked", "run_early", "run_same_state", "run_harness", "run_silent")
ASK_PHRASES = ("waiting for", "let me know", "should i", "would you like")
EARLY_FRACTION = 0.10
DAY = 86400
PER_CARD = 2                                   # resume or re-dispatch continues per card per 24 h
HARNESS_STEP = re.compile(r"\buntrack(?:ed|ing)?\b", re.I)
SKIP_BLOCKS = ("parked", "spec_stale", "guide_ruling", "runaway", "claims")



def _key(task_id):
    return f"continue:{task_id}"


def asks(text):
    """The text ends with "?" or holds one of ASK_PHRASES."""
    low = " ".join(str(text or "").lower().split())
    return low.endswith("?") or any(p in low for p in ASK_PHRASES)


def kind(run, before, after, handoff, last_text):
    """The quiet-stop kind of one closed run, or None. `run`: error_class, exit, outcome,
    elapsed_s, cap_s. `before` and `after`: the progress marks (`mark`: head, block_kind,
    findings, verdict, check as "pass" or "fail"). `handoff`: the run's document (verdict, block,
    question, answers, left_undone or remaining). A head that is None is unknown: no rule that
    needs the head fires."""
    run, before, after, h = run or {}, before or {}, after or {}, handoff or {}
    if run.get("error_class") or run.get("exit"):
        return None
    left = _items(h, "left_undone", "remaining")
    if any(HARNESS_STEP.search(t) for t in left + [str(after.get("block_reason") or "")]):
        return "run_harness"
    known = before.get("head") is not None and after.get("head") is not None
    same = known and before["head"] == after["head"]
    if h.get("question") or (after.get("block_kind") == "needs_input"
                             and run.get("worker_block")):
        return "run_asked"
    if asks(last_text) and (same or after.get("check") == "fail"):
        return "run_asked"
    cap, took = run.get("cap_s"), run.get("elapsed_s")
    if (cap and took is not None and took < EARLY_FRACTION * cap
            and (after.get("check") == "fail" or left)):
        return "run_early"
    answered = h.get("answers") or after.get("findings") != before.get("findings", after.get(
        "findings"))
    if same and not (h.get("verdict") or h.get("block") or answered):
        return "run_empty"
    keys = ("head", "block_kind", "findings", "verdict")
    if known and all(before.get(k) == after.get(k) for k in keys):
        return "run_same_state"
    return None


def mark(conn, task_id, head, handoff=None, until=None):
    """The card's progress mark after a run: block kind, branch head, count of review-failed
    notes (those up to `until` when given), the run's verdict."""
    card = conn.execute("SELECT block_kind FROM tasks WHERE id=?", (task_id,)).fetchone()
    findings = conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=? AND author="
                            "'capsule' AND body LIKE 'Review failed:%' AND created_at<=?",
                            (task_id, until if until is not None else 2**62)).fetchone()[0]
    return {"block_kind": card["block_kind"] if card else None, "head": head,
            "findings": findings, "verdict": (handoff or {}).get("verdict")}


def _ws(tick, cfg, branch):
    root = getattr(tick, "_work_root", None)
    return Path(root(cfg)) / workspace._slug(branch) if root and branch else None


EMPTY = hashlib.sha256(b"").hexdigest()


def _head(ws, base):
    """The work mark of the clone: a hash of its diff against `base` (`start_sha`), tracked and
    untracked files, the same diff the review sees. The container git is read-only and the harness
    commits only after the gate, so HEAD cannot show a run's work. None: unknown."""
    try:
        if ws is None or not base:
            return None
        return hashlib.sha256(workspace.diff(ws, base).encode("utf-8", "replace")).hexdigest()
    except Exception:
        return None


def _done(conn, pk):
    """The run ended with a done report (`request_review`): never a quiet stop, review decides."""
    return conn.execute("SELECT 1 FROM task_events WHERE run_id=? AND kind='request_review'",
                        (pk,)).fetchone() is not None


def on_close(tick, conn, cfg, pk, sp, now, worker_block=False):
    """After an implementer run closed: its session id in the run record, one `run_closed` event
    with the marks and the kind. `worker_block`: the run ended itself with a `block` request (not a
    hold the tick made from its report). Never raises: a card's close must not fail here."""
    try:
        _on_close(tick, conn, cfg, pk, sp, now, worker_block)
    except Exception as exc:
        print(f"capsule stall_continue: on_close: {type(exc).__name__}: {exc}", file=sys.stderr)


def _on_close(tick, conn, cfg, pk, sp, now, worker_block):
    run = conn.execute("SELECT * FROM task_runs WHERE id=?", (pk,)).fetchone()
    if run is None or run["profile"] != "implementer" or run["ended_at"] is None:
        return
    run_json = _json((Path(sp) / "run.json").read_text()) if (Path(sp) / "run.json").exists() \
        else {}
    run_json = run_json or {}
    sid = run_json.get("session_id")
    if sid:
        conn.execute("UPDATE task_runs SET session_id=? WHERE id=?", (str(sid)[:64], pk))
    card = conn.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
    handoff = _json(run["handoff"])
    meta = _json(run["metadata"]) or {}
    after = mark(conn, run["task_id"], _head(_ws(tick, cfg, card["branch_name"]),
                                             meta.get("start_sha")), handoff)
    prev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='run_closed'"
                        " ORDER BY id DESC LIMIT 1", (run["task_id"],)).fetchone()
    old = (_json(prev[0]) or {}).get("after") or {} if prev else {}
    before = dict(old or after, head=(old.get("head") or EMPTY) if after["head"] is not None
                  else None,         # the last close's block kind and verdict
                  findings=mark(conn, run["task_id"], None, until=run["started_at"])["findings"])
    after["check"] = None                    # no check runs before review: the oracle is unknown
    blocked = run["outcome"] == "blocked"
    if blocked:
        row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'"
                           " ORDER BY id DESC LIMIT 1", (run["task_id"],)).fetchone()
        after["block_reason"] = (_json(row[0]) or {}).get("reason") if row else None
    cap = (cfg.get("caps") or {}).get("max_runtime_seconds", config.CAP_DEFAULTS[
        "max_runtime_seconds"])
    info = {"error_class": run["error_class"], "exit": run_json.get("exit"),
            "outcome": run["outcome"], "worker_block": worker_block,
            "elapsed_s": max(0, run["ended_at"] - run["started_at"]),
            "cap_s": card["max_runtime_seconds"] or cap}
    text = str(run_json.get("last_text") or "") or str((after.get("block_reason")) or "")
    got = None if _done(conn, pk) else kind(info, before, after, handoff, text)
    event(conn, run["task_id"], "run_closed",
          {"role": "implementer", "outcome": run["outcome"], "kind": got, "session_id": sid,
           "before": before, "after": after}, run_pk=pk, run_id=run["run_id"], now=now)


# -- the step --------------------------------------------------------------------------------

def _counts(conn, now):
    """(per card {id: [continue times in 24 h since the card's last progress]}, board total in
    24 h). A run that made progress (`_reset`) starts the card's own count again."""
    per, total = {}, 0
    for row in conn.execute("SELECT id, task_id, created_at, payload FROM task_events WHERE kind="
                            "'stall_action' AND created_at>?", (now - DAY,)):
        p = _json(row[3]) or {}
        if p.get("action") == "continue":
            total += 1
            if row[0] > (guard_get(conn, _key(row[1]))[0] or {}).get("reset_ev", 0):
                per.setdefault(row[1], []).append((row[2], p.get("path")))
    return per, total


def _in_review(tick, conn, card, run, now):
    """A review pass of this card has started: a review-role run after the run, or a contract
    job running or backing off."""
    jobs = getattr(tick, "contracts", None)
    return bool(conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND id>? AND profile IN"
                             " ('validator','reviewer','guide','security') LIMIT 1",
                             (card["id"], run["id"])).fetchone()
                or (jobs is not None and jobs.running(card, run, now)))


def _eligible(tick, conn, card, run, quiet, now):
    """The card may take a continue now: status, hold, safety and open run. A card the tick held
    from the run's report stays held, except for a question or a step the harness does itself."""
    if card["status"] not in ("review", "ready", "blocked"):
        return False
    if card["status"] == "review" and _in_review(tick, conn, card, run, now):
        return False
    aside = guard_get(conn, f"aside:{card['id']}")[0]       # lasts until a human lifts the card
    if aside and not conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind IN ('unblocked','failure_reset')"
            " AND created_at>=? LIMIT 1", (card["id"], aside.get("since") or 0)).fetchone():
        return False
    if card["status"] == "blocked" and (card["block_kind"] != "needs_input"
                                        or quiet not in ("run_asked", "run_harness")):
        return False
    if card["block_kind"] in SKIP_BLOCKS or card["request_needs"] == "decision":
        return False
    if conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL",
                    (card["id"],)).fetchone():
        return False
    row = conn.execute("SELECT handoff FROM task_runs WHERE task_id=? AND profile='reviewer' AND"
                       " handoff IS NOT NULL ORDER BY id DESC LIMIT 1", (card["id"],)).fetchone()
    doc = _json(row[0]) if row else None
    found = (doc or {}).get("findings", [])
    return not any(isinstance(f, dict) and f.get("safety") for f in found)


def _can_strong(cfg, rec, now):
    return (restart.ROLE in (cfg or {}).get("routing", {})
            and (rec.get("strong_at") is None or now - rec["strong_at"] >= DAY))


def _aside(conn, cfg, card, now):
    """Step 4: the lane moves on; one incident, no page."""
    guard_set(conn, f"aside:{card['id']}", {"since": now, "reason": "quiet stop: continue failed"},
              now=now)
    line = "quiet stop: continue failed (resume, re-dispatch and the stronger tier tried)"
    cause = conn.execute("SELECT id FROM incidents WHERE task_id=? AND status NOT IN ('closed',"
                         "'superseded') ORDER BY opened_at DESC LIMIT 1", (card["id"],)).fetchone()
    if cause:
        incidents.attach(conn, cause[0], [line])
        return
    run = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (card["id"],)).fetchone()[0]
    board = cfg.get("board") or "board"
    incidents.open_incident(conn, board=board, kind="hang", task_id=card["id"], now=now,
                            key=f"stall: quiet_stop: {stall_page.tag([card['id'], run or 0])}",
                            description=f"{board}: {line}", evidence=[line])


def _continue(tick, conn, cfg, card, run, ev, rec, path, strong, now):
    """Step 1 to 3: the plan, the guide-ruling note, the card back to ready, the event."""
    text = missing(conn, card, run)
    sid = run["session_id"] if "session_id" in run.keys() else None
    after = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_runs WHERE task_id=?",
                         (card["id"],)).fetchone()[0]
    plan = {"strong": bool(strong), "after": after}
    if path == "resume":
        plan.update(session_id=sid, prompt="Continue the task. You stopped without finishing: "
                    + text)
    with txn(conn):
        if card["status"] == "blocked":
            if not blocks.unblock(conn, card["id"], by="worker", now=now):
                return False
        elif card["status"] == "review":
            transition(conn, card["id"], "ready", frm=("review",), claim=True, current_run_id=None)
        if path != "resume":
            event(conn, card["id"], "guide_ruling",
                  {"choice": "continue", "reason": text[:2000], "role": "implementer",
                   "applied": True}, now=now)
        guard_set(conn, f"continue_plan:{card['id']}", plan, now=now)
        rec["n"] = rec.get("n", 0) + 1
        if strong:
            rec["strong_at"] = now
        event(conn, card["id"], "stall_action",
              {"action": "continue", "path": "stronger" if strong else path,
               "kind": ev.get("kind"), "from_run": run["id"]}, now=now)
    return True


def _worktree_same(tick, cfg, card, ev, run=None):
    ws = _ws(tick, cfg, card["branch_name"])
    want = (ev.get("after") or {}).get("head")
    base = (_json(run["metadata"]) or {}).get("start_sha") if run is not None else None
    return ws is None or want is None or _head(ws, base) == want


def _decide(tick, conn, cfg, card, run, ev, rec, per, now):
    """The ladder: resume, re-dispatch, stronger tier, set aside. 'skip' leaves the quiet stop
    for a later pass (a cap), True when a continue was prepared."""
    used = [path for (_, path) in per.get(card["id"], ())]
    cheap = [p for p in used if p in ("resume", "redispatch")]
    if len(cheap) < PER_CARD:
        sid = run["session_id"] if "session_id" in run.keys() else None
        provider = (run["provider"] if "provider" in run.keys() else None) or "claude"
        can = sid and provider == "claude" and rec.get("n", 0) == 0 and _worktree_same(
            tick, cfg, card, ev, run)
        return _continue(tick, conn, cfg, card, run, ev, rec,
                         "resume" if can else "redispatch", False, now)
    if _can_strong(cfg, rec, now):
        return _continue(tick, conn, cfg, card, run, ev, rec, "redispatch", True, now)
    _aside(conn, cfg, card, now)
    return False


def _closes(conn, seen):
    """Each unseen close, oldest first: (cursor, id, task id, payload, run row). `run_closed`
    events (cursor "events") and a run ended `run_silent` with no such event (cursor "runs")."""
    out = []
    for row in conn.execute("SELECT id, task_id, payload, run_id FROM task_events WHERE id>? AND"
                            " kind='run_closed' ORDER BY id", (seen["events"],)):
        run = conn.execute("SELECT * FROM task_runs WHERE id=?", (row[3],)).fetchone()
        if run is not None:
            out.append(("events", row[0], row[1], _json(row[2]) or {}, run))
    for run in conn.execute("SELECT * FROM task_runs WHERE outcome='run_silent' AND id>? AND"
                            " profile='implementer' AND ended_at IS NOT NULL ORDER BY id",
                            (seen["runs"],)):
        if not conn.execute("SELECT 1 FROM task_events WHERE run_id=? AND kind='run_closed'",
                            (run["id"],)).fetchone():
            out.append(("runs", run["id"], run["task_id"], {"kind": "run_silent"}, run))
    return out


def step(tick, conn, cfg, now):
    """One pass: continue each card whose newest implementer run closed quiet. Never raises."""
    try:
        _step(tick, conn, cfg, int(now))
    except Exception as exc:
        print(f"capsule stall_continue: step: {type(exc).__name__}: {exc}", file=sys.stderr)


def _reset(conn, task_id, ev, per, now):
    """A run that made progress (its work mark moved): the card's continue count is 0 again, for
    the ladder and for the 24 h per-card cap (only later continues count)."""
    if (ev.get("after") or {}).get("head") != (ev.get("before") or {}).get("head"):
        rec = guard_get(conn, _key(task_id))[0] or {}
        top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE kind='stall_action'"
                           ).fetchone()[0]
        guard_set(conn, _key(task_id), {**{k: v for k, v in rec.items() if k == "strong_at"},
                                        "reset_ev": top}, now=now)
        per.pop(task_id, None)


def _step(tick, conn, cfg, now):
    from capsule import dispatch      # dispatch imports restart, which imports this module
    if dispatch.any_pause(conn, now):          # pause:board, an account pause or weekly_pause
        return
    seen = {"events": 0, "runs": 0, **(guard_get(conn, "continue_seen")[0] or {})}
    closes = _closes(conn, seen)
    per, total = _counts(conn, now)
    cap = config.unattended(cfg)["continues_per_day"]
    last = {c[2]: (c[0], c[1]) for c in closes}
    for cursor, eid, task_id, ev, run in closes:
        seen[cursor] = max(seen[cursor], eid)
        if ev.get("kind") is None:
            _reset(conn, task_id, ev, per, now)
            continue
        card = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        later = conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND id>?",
                             (task_id, run["id"])).fetchone()
        if (card is None or last[task_id] != (cursor, eid) or total >= cap or later
                or not _eligible(tick, conn, card, run, ev.get("kind"), now)
                or pending(conn, task_id)):
            continue
        rec = guard_get(conn, _key(task_id))[0] or {}
        if _decide(tick, conn, cfg, card, run, ev, rec, per, now):
            total += 1
        guard_set(conn, _key(task_id), rec, now=now)
    guard_set(conn, "continue_seen", seen, now=now)


# -- the dispatcher's side ------------------------------------------------------------------

def plan(conn, task_id, run=None):
    """The prepared continue of a card, or None: `strong`, and for a resume `session_id` and
    `prompt`. It belongs to the first run that starts after it was made (`after`, the newest run id
    then): a later run, or a run other than `run`, gets none, so no clear step is needed."""
    got = guard_get(conn, f"continue_plan:{task_id}")[0]
    if not got:
        return None
    newer = [r[0] for r in conn.execute("SELECT id FROM task_runs WHERE task_id=? AND id>?"
                                        " ORDER BY id", (task_id, got["after"]))]
    return got if (not newer if run is None else newer == [run["id"]]) else None


def pending(conn, task_id):
    """A prepared continue whose run has not started yet."""
    got = guard_get(conn, f"continue_plan:{task_id}")[0]
    return bool(got) and not conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND id>?",
                                          (task_id, got["after"])).fetchone()
