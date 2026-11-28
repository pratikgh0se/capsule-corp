"""Stall supervisor, unit 3: the runs, review and landing detectors and their L0 remedies (see
docs/architecture/components/stall-supervisor.md). `stall` registers `DETECTORS` and calls `step`.
L0: at most `L0_TRIES` per stall, `L0_GAP_S` apart, one `stall_action` event each. `probes` (all
optional, injected by a test): `alive(pid)`, `quiet_s(run_id)`, `cpu_s(pid)`, `kill(pid)`,
`docker_up` (a bool or a callable), `spool_root`."""
import json

from capsule import blocks, config, host_guard, lease, seats, stall_probes
from capsule.db import event, guard_get, guard_set, transition, txn
from capsule.stall_silence import (  # noqa: F401
    CPU_GROWTH_S, KEEP_S, SAMPLE_GAP_S, SAMPLES, _container, _sample, _silence, _window)

L0_TRIES, L0_GAP_S, RECHECKS, RECHECK_AGAIN_S = 2, 600, 3, 6 * 3600
KILLS_PER_DAY, DAY = 2, stall_probes.DAY
UNSPAWNED_S, DEFERRED_S, LANDING_S, ORPHAN_S = 300, 1800, 1800, 300
WAIT_FETCH_ENV_S = 1800
PHRASE = {"review_deferred": "waits for a review seat", "state_orphan": "is marked running"
          " but nothing runs it", "landing_stuck": "is approved but does not land"}
RUN_CLASSES = ("run_silent", "run_unspawned", "lander_stuck", "failure_storm")
STUCK_WORDS = ("idle", "running", "off")
MARKS = ",".join("?" * len(seats.REVIEW_PROFILES))
HOLDS = ",".join(f"'{k}'" for k in blocks.HOLD_EVENTS)
UNSPAWNED_Q = ("SELECT * FROM task_runs WHERE ended_at IS NULL AND worker_pid IS NULL"  # noqa: S608
               f" AND profile IN ({MARKS}) AND started_at<=?"
               " AND json_extract(metadata, '$.setup') IS NULL")     # a queued tester setup
SEATS_Q = ("SELECT profile, run_id, task_id FROM task_runs WHERE ended_at IS NULL"  # noqa: S608
           f" AND profile IN ({MARKS})")
WAIT_Q = ("SELECT created_at FROM task_events WHERE task_id=? AND kind IN"  # noqa: S608
          f" ({HOLDS}) ORDER BY id DESC LIMIT 1")


def _item(cls, subject, since, evidence, grace, l2_after, **extra):
    return {"class": cls, "subject": subject, "since": since, "evidence": evidence,
            "grace": grace, "l2_after": l2_after, **extra}


def _val(x):
    return x() if callable(x) else x


def _cards(conn, cfg, where, params=()):
    """Cards matching `where` on an active lane (`epics[]`, as the card classes of `stall` do): a
    legacy card on no live lane stalls nothing and gets no remedy."""
    active = set(config.epic_branches({"epics": [], **cfg}))
    return [c for c in conn.execute(f"SELECT * FROM tasks t WHERE {where}", params)  # noqa: S608
            if c["branch_name"] in active]


def _rec(conn, cls, subject):
    return guard_get(conn, f"stall:{cls}:{subject}")[0] or {}


def _l2(conn, cfg, cls, subject, can):
    """The age at which a stall pages: the deadline, or None while an L0 remedy may still fix it."""
    if can and _rec(conn, cls, subject).get("l0", 0) < L0_TRIES:
        return None
    return config.stall(cfg)["l2_deadline_s"]


def _kills(conn, card, now):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='stall_action' AND created_at>?"
        " AND json_extract(payload, '$.action')='kill'", (card, now - DAY)).fetchone()[0]


# -- detectors --------------------------------------------------------------------------------

def _run_silent(conn, cfg, now, probes):
    """C1: a live worker with no file growth and no CPU growth over the stored window."""
    p, out = stall_probes.resolve(conn, cfg, now, probes), []
    for run in conn.execute("SELECT * FROM task_runs WHERE ended_at IS NULL AND run_id IS NOT NULL"
                            " AND worker_pid IS NOT NULL"):
        got = _silence(conn, cfg, now, p, run)
        if got is None:
            continue
        quiet, state, grew = got
        will = (config.stall(cfg)["silent_kill"] and state != "unreadable"
                and not _container(run) and _kills(conn, run["task_id"], now) < KILLS_PER_DAY)
        cpu = ("container run: observe only" if _container(run) else "cpu: unreadable"
               if state == "unreadable" else "cpu: window not proven yet"
               if state == "watch" else f"cpu: {grew:.1f}s over the quiet window")
        out.append(_item("run_silent", run["run_id"], now - int(quiet),
                         [f"role: {run['profile']}", f"card: {run['task_id']}", cpu], 0,
                         _l2(conn, cfg, "run_silent", run["run_id"], will)))
    return out


def _run_unspawned(conn, cfg, now, probes):
    """C3: a review-role run row with no worker pid (it holds a seat for nothing)."""
    rows = conn.execute(UNSPAWNED_Q, (*seats.REVIEW_PROFILES, now - UNSPAWNED_S)).fetchall()
    return [_item("run_unspawned", r["run_id"] or str(r["id"]), r["started_at"],
                  [f"role: {r['profile']}", f"card: {r['task_id']}", "no worker pid"], 0,
                  _l2(conn, cfg, "run_unspawned", r["run_id"] or str(r["id"]), True))
            for r in rows]


def _state_orphan(conn, cfg, now, probes):
    """C4: a card marked running with no open run and a claim that ended ORPHAN_S ago."""
    rows = _cards(conn, cfg,
                  "status IN ('running','claimed') AND claim_expires IS NOT NULL AND"
                  " claim_expires<=? AND NOT EXISTS (SELECT 1 FROM task_runs r WHERE"
                  " r.task_id=t.id AND r.ended_at IS NULL)", (now - ORPHAN_S,))
    return [_item("state_orphan", r["id"], r["claim_expires"], ["no open run"], 0, 0)
            for r in rows]


def _deferred_since(conn, card_id):
    """Start of the card's trailing `review_deferred` run of events (stall events do not count)."""
    since = None
    for kind, at in conn.execute("SELECT kind, created_at FROM task_events WHERE task_id=?"
                                 " ORDER BY id DESC", (card_id,)):
        if kind.startswith("stall_"):
            continue
        if kind != "review_deferred":
            break
        since = at
    return since


def _seat_holders(conn, now):
    held = [f"seat: {r['profile']} run {r['run_id']} (card {r['task_id']})" for r in conn.execute(
        SEATS_Q, seats.REVIEW_PROFILES)]
    busy = guard_get(conn, "lander.busy")[0]
    if busy and busy.get("busy"):
        held.append(f"seat: lander (card {busy.get('task')}) since {busy.get('at')}")
    return held


def _dead_lander(conn, now):
    busy = guard_get(conn, "lander.busy")[0]
    return bool(busy and busy.get("busy") and now - busy["at"] >= seats.LANDER_TTL)


def _review_deferred(conn, cfg, now, probes):
    """D1: a card in review whose newest event is `review_deferred`, for DEFERRED_S."""
    out = []
    for card in _cards(conn, cfg, "status='review'"):
        since = _deferred_since(conn, card["id"])
        if since is None or now - since < DEFERRED_S:
            continue
        can = _dead_lander(conn, now)
        out.append(_item("review_deferred", card["id"], since, _seat_holders(conn, now) or
                         ["seat: none open"], 0,
                         _l2(conn, cfg, "review_deferred", card["id"], can)))
    return out


def _rechecks(conn, card):
    last = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(created_at), 0) FROM task_events WHERE task_id=? AND"
        " kind='stall_action' AND json_extract(payload, '$.action')='recheck' AND id>"
        "(SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=? AND kind='completed')",
        (card, card)).fetchone()
    return last[0], last[1]


def _wait_since(conn, card):
    row = conn.execute(WAIT_Q, (card,)).fetchone()
    return row[0] if row else None


def _landing_stuck(conn, cfg, now, probes):
    """E1: a `pushed` landing held LANDING_S, or a lander wait that rechecks did not settle."""
    out, active = [], set(config.epic_branches({"epics": [], **cfg}))
    for key, raw in conn.execute(     # only the pushed rows (a superset by text), not every landing
            "SELECT key, value FROM guard_state WHERE key LIKE 'lander.landed:%'"
            " AND value LIKE '%pushed%'").fetchall():
        try:
            held = json.loads(raw)
        except ValueError:
            continue
        since = held.get("held_since") if isinstance(held, dict) else None
        card = key.split(":", 1)[1]
        row = conn.execute("SELECT branch_name FROM tasks WHERE id=?", (card,)).fetchone() \
            if held.get("state") == "pushed" and since and now - since >= LANDING_S else None
        if row and row["branch_name"] in active:
            out.append(_item("landing_stuck", card, since, ["landing: pushed, suites held"], 0, 0))
    for card in _cards(conn, cfg, "status='blocked'"):
        cause, since = blocks.lander_wait(conn, card["id"]), _wait_since(conn, card["id"])
        n = _rechecks(conn, card["id"])[0]
        if cause and since is not None and (n >= RECHECKS or now - since >= LANDING_S):
            out.append(_item("landing_stuck", card["id"], since, [f"wait: {cause}",
                             f"rechecks: {n}"], 0, 0))
    return out


def _lander_stuck(conn, cfg, now, probes):
    """E1: `landing.step` gave the same word, not idle or running, for LANDING_S."""
    last = guard_get(conn, "lander.last")[0] or {}
    word = last.get("word")
    if not word or word in STUCK_WORDS or now - last.get("since", now) < LANDING_S:
        return []
    if word in ("busy", "no_seat") and not _dead_lander(conn, now):
        return []                   # a live holder (the lander worker, a keeper, another board)
    return [_item("lander_stuck", "lander", last["since"], [f"lander: {word}"], 0,
                  config.stall(cfg)["l2_deadline_s"])]


DETECTORS = (_run_silent, _run_unspawned, _state_orphan, _review_deferred, _landing_stuck,
             _lander_stuck, *host_guard.DETECTORS)


# -- L0 remedies (each: (conn, cfg, tick, p, item, now) -> result text, or None when nothing to do)

def _action(conn, card, it, now, **fields):
    with txn(conn):
        event(conn, card or "stall", "stall_action",
              {"class": it["class"], "subject": it["subject"], **fields}, now=now)


def _end_unspawned(conn, row, now, why):
    with txn(conn):
        lease.end_run(conn, row["id"], status="failed", outcome="spawn_failed", error=why, now=now)
        event(conn, row["task_id"], "spawn_failed", {"role": row["profile"], "error": why},
              run_pk=row["id"], run_id=row["run_id"], now=now)


def _fix_unspawned(conn, cfg, tick, p, it, now):
    row = conn.execute("SELECT * FROM task_runs WHERE run_id=? AND ended_at IS NULL"
                       " AND worker_pid IS NULL", (it["subject"],)).fetchone()
    if row is None:
        return None
    _end_unspawned(conn, row, now, f"no worker started within {now - row['started_at']}s")
    _action(conn, row["task_id"], it, now, action="spawn_failed", result="ended")
    return "ended as spawn_failed"


def _kill_silent(conn, cfg, tick, p, it, now):
    """C1: re-check, kill the worker, end the run `timed_out` (`kind: run_silent`), as
    `Tick._timeout_one` does, but no failure is counted: an implementer card goes back to `ready`
    (the first step of a continue). Host runs only; a container run is never killed."""
    run = conn.execute("SELECT * FROM task_runs WHERE run_id=? AND ended_at IS NULL",
                       (it["subject"],)).fetchone()
    if (run is None or not config.stall(cfg)["silent_kill"]
            or _kills(conn, run["task_id"], now) >= KILLS_PER_DAY):
        return None
    got = _silence(conn, cfg, now, p, run)
    if got is None or got[1] != "proven" or _container(run):
        return None            # woke up, not proven, CPU unreadable or container: observe only
    if hasattr(tick, "_is_local") and not tick._is_local(run):
        return None                  # another host's run: never ours to end
    pid, quiet = run["worker_pid"], int(got[0])
    (p["kill"](pid) if "kill" in p else tick._kill(pid, run["run_id"]))
    if p["alive"](pid):
        return "kill failed: the worker is still alive"
    reason = (f"run {run['run_id']} ({run['profile']}) silent for {quiet}s: no file grew and the"
              f" worker used {got[2]:.1f}s of CPU in that time; killed")
    if hasattr(tick, "_apply_final_spool"):      # a verb written just before the kill lands
        run = tick._apply_final_spool(conn, tick._spool_root(cfg) / run["run_id"], run, now,
                                      {"applied": 0})
    with txn(conn):
        event(conn, run["task_id"], "timed_out", {"role": run["profile"], "elapsed": quiet,
              "worker_pid": pid, "kind": "run_silent"}, run_pk=run["id"],
              run_id=run["run_id"], now=now)
        if run["profile"] == "implementer":      # a kill starts a continue: no failure counted
            transition(conn, run["task_id"], "ready", frm=("running",), claim=True,
                       where=" AND current_run_id=?", params=(run["id"],))   # end_run clears it
        if run["ended_at"] is None:
            lease.end_run(conn, run["id"], status="timed_out", outcome="run_silent", error=reason,
                          now=now)
    _action(conn, run["task_id"], it, now, action="kill", result="timed_out", pid=pid,
            kind="run_silent")
    return f"killed pid {pid}, run ended timed_out"


def _free_seats(conn, cfg, tick, p, it, now):
    """D1: clear a `lander.busy` flag past its TTL (rows with no pid are `run_unspawned`)."""
    if not _dead_lander(conn, now):
        return None
    task = guard_get(conn, "lander.busy")[0].get("task")
    guard_set(conn, "lander.busy", {"busy": False, "task": task, "at": now}, now=now)
    _action(conn, None, it, now, action="free_lander", result="cleared")
    return "cleared an expired lander.busy flag"


def infra_rerun_used(conn, task_id):
    """The card already had its one automatic review re-run since the last other lift."""
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='unblocked' AND"
        " json_extract(payload, '$.by')='capsule' AND id>COALESCE((SELECT MAX(id) FROM"
        " task_events WHERE task_id=? AND kind IN ('unblocked','failure_reset') AND"
        " json_extract(payload, '$.by') IS NOT 'capsule'), 0)",
        (task_id, task_id)).fetchone() is not None


def _rerun_infra(conn, cfg, tick, p, it, now):
    """D2: one automatic re-run of the failed review role (the card goes back to review)."""
    card = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?",
                        (it["subject"],)).fetchone()
    if card is None or card["status"] != "blocked" or card["block_kind"] != "review_infra":
        return None
    if infra_rerun_used(conn, it["subject"]):
        return None                              # one automatic re-run, then the owner
    got = blocks.unblock(conn, it["subject"], by="capsule", now=now)
    _action(conn, it["subject"], it, now, action="rerun_review",
            result="unblocked" if got else "refused")
    return "re-ran the review" if got else "unblock refused"


REMEDIES = {"run_silent": _kill_silent, "run_unspawned": _fix_unspawned,
            "review_deferred": _free_seats, "held_blocked": _rerun_infra}


def _bump(conn, it, now, result):
    key = f"stall:{it['class']}:{it['subject']}"
    rec = guard_get(conn, key)[0]
    if rec is not None:
        guard_set(conn, key, dict(rec, l0=rec.get("l0", 0) + 1, l0_at=now, l0_result=result),
                  now=now)


def _l0(conn, cfg, tick, p, items, now):
    for it in items:
        fn = REMEDIES.get(it["class"])
        rec = _rec(conn, it["class"], it["subject"])
        if fn is None or rec.get("l0", 0) >= L0_TRIES or now - rec.get("l0_at", 0) < L0_GAP_S:
            continue
        result = fn(conn, cfg, tick, p, it, now)
        if result:
            _bump(conn, it, now, result)


def _recheck_waits(conn, cfg, p, now):
    """E1: a card blocked on a lander wait goes back to the landing check when the awaited thing
    is back (Docker up for a docker wait; 30 min for a fetch or env wait), RECHECKS times, then once
    per RECHECK_AGAIN_S. Never a forced landing."""
    for card in _cards(conn, cfg, "status='blocked'"):
        cause, since = blocks.lander_wait(conn, card["id"]), _wait_since(conn, card["id"])
        if cause is None or since is None:
            continue
        n, last = _rechecks(conn, card["id"])
        back = (_val(p["docker_up"]) if cause == "lander_wait_docker"
                else now - since >= WAIT_FETCH_ENV_S)
        if not back or now - last < (L0_GAP_S if n < RECHECKS else RECHECK_AGAIN_S):
            continue
        if blocks.unblock(conn, card["id"], by="capsule", now=now):
            _action(conn, card["id"], {"class": "landing_stuck", "subject": card["id"]}, now,
                    action="recheck", result="done", n=n + 1, cause=cause)


RUN_TEXT = {"run_silent": "a worker is alive but wrote nothing", "lander_stuck":
            "the lander has not moved", "run_unspawned": "a review worker never started",
            "failure_storm": "runs keep failing to start or crash: the board is paused"}


def step(tick, conn, cfg, items, now, probes=None):
    """The L0 pass: remedies for `items` (the open stalls), then the E1 recheck for every card.
    (`stall._run` calls `route_defaults`, B3, before it looks for stalls.)"""
    p = stall_probes.resolve(conn, cfg, now, probes, tick)
    _sample(conn, p, now)
    _l0(conn, cfg, tick, p, items, now)
    _recheck_waits(conn, cfg, p, now)
