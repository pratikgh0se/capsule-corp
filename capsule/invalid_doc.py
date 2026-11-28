"""Invalid-document handling for a closed run (row 507(b)): a role's
own requests/ document was rejected as invalid (spool.py `_mark_invalid`), never
evidence the WORK itself failed."""
import json
import uuid

from capsule import blocks, config, db, incidents, lease, outbox, restart
from capsule.db import event, txn
from capsule.runners import host_wall

OUTAGE_KEY = "wall:outage"       # + ":<role>": one outage per failing role
TIMEOUTS_KEY = "wall:timeouts"
TIMEOUT_PAGE = 3                 # start-up checks that time out in a row before a person is paged
OUTAGE_GAP = 1800       # seconds: a wall failure this long after the last one is a new outage


FAILED_REASON_CAP = 300


def failed_report_reason(report):
    """Why a report with status `failed` failed: its own `error`, else "the run reported failed:"
    and the first `issues` line (else the first `left_undone` line), whitespace-collapsed and cut
    to FAILED_REASON_CAP characters. A report with neither keeps the bare text."""
    if report.get("error"):
        return report["error"]
    first = next((" ".join(str(x).split()) for k in ("issues", "left_undone")
                  for x in report.get(k) or [] if str(x).strip()), "")
    return "the run reported failed" + (f": {first[:FAILED_REASON_CAP]}" if first else "")


def close_failed_run(conn, cfg, run, run_json, error_class, role, task_id, review, why, is_invalid,
                     now):
    """Ends a "report is None" run (never a `worktree_mismatch`, which `tick.py`
    `_close` returns on earlier) and routes it: a crash gets its typed `crashed`
    event; an invalid_doc retries once (never touching `blocks.record_failure`) or
    parks on the second miss in a row (`retry` below); anything else is an ordinary
    `blocks.record_failure`. A run the host wall refused for its own job or tier: `_wall_held`."""
    if error_class in host_wall.HOLD_CLASSES:
        return _wall_held(conn, run, run_json, task_id, review, now)
    with txn(conn):
        if run_json is None or error_class == "fatal":     # row 456: a crash is a typed event
            event(conn, task_id, "crashed", {"role": role, "error_class": error_class or "fatal",
                                             "exit": (run_json or {}).get("exit"),
                                             # row 482: the tool(s) a denied tool_use crash
                                             # names, and the CLI's own denial reason
                                             "tool": (run_json or {}).get("tool"),
                                             "denied_tools": (run_json or {}).get("denied_tools") or [],
                                             "denial_reason": (((run_json or {}).get("denial_reason")
                                                               or "")[:500] or None),
                                             "error": ((run_json or {}).get("error") or why)[:500]},
                  run_pk=run["id"], run_id=run["run_id"], now=now)
        conn.execute("UPDATE task_runs SET error_class='invalid_output' WHERE id=? AND error_class IS NULL",
                     (run["id"],))
        lease.end_run(conn, run["id"], status="failed",
                      outcome="invalid_document" if is_invalid else "failed", error=why[:2000],
                      summary=None if is_invalid else why[:2000], now=now)
        restarted = None
        if is_invalid and not review:
            restarted = retry(conn, cfg, run, why, now)
        elif not review:
            text = (run_json or {}).get("error") or why
            blocks.record_failure(conn, task_id, reason=f"run {run['run_id']}: {why}",
                                  limit=restart.failure_cap(cfg), run_id=run["run_id"], now=now,
                                  git_lock=blocks.mentions_git_lock(text))
    return restarted


def refusal(run_json):
    """The CLI's own refusal text (at most 300 characters) of a run the account refused, else ''."""
    doc = run_json or {}
    return str(doc.get("error") or "")[:300] if doc.get("error_class") == "account" else ""


def pause_scope(conn, board, run, run_json, now):
    """(pause guard keys, plain reason) for a run that ended asking the tick to pause. A rate limit
    pauses the board and its account. A host wall that cannot be applied (D161) pauses the board
    alone, and opens one incident keyed on that failure, not on the card: the card costs no round
    and the tick retries when the pause ends. A start-up check that timed out is no failure of the
    wall: it pauses nothing (no keys), see `_timeouts`."""
    error_class = (run_json or {}).get("error_class")
    if error_class == host_wall.TIMEOUT_CLASS:
        return [], _timeouts(conn, board, run, now)
    if error_class != host_wall.ERROR_CLASS:
        account = run["account"]
        what = "Claude login lost" if error_class == "auth_required" else "rate limit"
        if error_class == "account":
            what = "account refused"
        return (["pause:board"] + ([f"pause:account:{account}"] if account else []),
                f"{what} on {account or 'the account'}")
    why = ((run_json or {}).get("error") or "host wall unavailable")[:500]
    incidents.open_incident(conn, board=board, kind="review_infra", role="host_wall",
                            key=host_wall.ERROR_CLASS, task_id=run["task_id"],
                            run_id=run["run_id"], now=now,
                            evidence=[why, f"{run['profile']} run {run['run_id']}"])
    until = int((run_json or {}).get("retry_after") or now + host_wall.WALL_RETRY)
    held, _ = db.guard_get(conn, "pause:board")     # `wall` marks it for `dispatch.any_pause`
    held = held if isinstance(held, dict) and held.get("until", 0) > now else {}
    guard = held.get("reason") in ("disk", "failure_storm")
    if guard:                       # keep the guard's end only: the wall must outlive its lift
        held = {"until": held["until"]}
    # a live pause nobody needs to be paged about
    person = bool(held) and "wall" not in held and not guard
    since = held.get("since", now) if person else _outage(conn, board, run, why, now)
    # `since` is the outage's start, so the digest's "paused for" counts the outage, not a retry;
    # a person's pause (live, no `wall`) is only ever extended, never marked as the wall's
    mark = {} if person else {"wall": since, "since": held.get("since", since)}
    db.guard_set(conn, "pause:board", {**held, "until": max(until, held.get("until", 0)), **mark},
                 now=now)
    return ["pause:board"], f"{why} (the tick retries when the pause ends)"


def _succeeded(conn, since, roles):
    """True when a session of one of `roles` has ended well after `since` (its wall worked)."""
    marks = ",".join("?" * len(roles))
    return conn.execute(
        f"SELECT 1 FROM task_runs WHERE ended_at > ? AND profile IN ({marks}) AND"
        " outcome IN ('completed', 'checkpoint', 'contradiction', 'ruling')",
        (since, *roles)).fetchone() is not None


def _timeouts(conn, board, run, now):
    """Count start-up checks that timed out in a row (a start that worked, in any host role, or
    `OUTAGE_GAP` of quiet starts the count again). Such a start is dropped and tried again by the
    next pass; it says nothing about the wall, so nothing pauses. The `TIMEOUT_PAGE`th in a row
    opens one incident and pages once (a later one in the same run of them sends nothing)."""
    held, _ = db.guard_get(conn, TIMEOUTS_KEY)
    n, last = (held.get("n", 0), held.get("last", 0)) if isinstance(held, dict) else (0, 0)
    fresh = not last or now - last > OUTAGE_GAP or _succeeded(conn, last, host_wall.HOST_ROLES)
    n = 1 if fresh else n + 1
    db.guard_set(conn, TIMEOUTS_KEY, {"n": n, "last": now}, now=now)
    if n == TIMEOUT_PAGE:
        incidents.open_incident(conn, board=board, kind="review_infra", role="host_wall",
                                key=host_wall.TIMEOUT_CLASS, task_id=run["task_id"],
                                run_id=run["run_id"], now=now,
                                evidence=[f"{run['profile']} run {run['run_id']}"])
        outbox.notify(conn, "incident", f"{board}: the host wall's start-up check timed out"
                      f" {n} starts in a row (an overloaded machine?); those sessions were not"
                      " started and the tick tries again by itself", page=True,
                      fields=[("board", board)], thread_key=run["task_id"], now=now)
    return "the wall's start-up check timed out; the start was dropped and is tried again"


def outage_open(conn, role, now):
    """True while `role`'s wall outage is on: a failure within `OUTAGE_GAP`, and no session of that
    role has ended well since (its wall worked again)."""
    held, _ = db.guard_get(conn, f"{OUTAGE_KEY}:{role}")
    last = held.get("last", 0) if isinstance(held, dict) else 0
    return bool(last) and now - last <= OUTAGE_GAP and not _succeeded(conn, last, [role])


def _outage(conn, board, run, why, now):
    """When this wall outage began, per role (a Korin-only outage is not ended by an implementer
    that works). A failure starts a new one when none is held for the role, when the last failure
    is `OUTAGE_GAP` old, or when a session of that same role has succeeded since the last failure
    (its wall worked again, `outage_open`). A person is paged once per outage, through the class
    that breaks quiet hours: when no role has one open. Its retries send nothing more."""
    key = f"{OUTAGE_KEY}:{run['profile']}"
    held, _ = db.guard_get(conn, key)
    page = not any(outage_open(conn, role, now) for role in host_wall.HOST_ROLES)
    since = held.get("since", now) if outage_open(conn, run["profile"], now) else now
    db.guard_set(conn, key, {"since": since, "last": now}, now=now)
    if page:
        retry = f"every {host_wall.WALL_RETRY // 60} min"
        outbox.notify(conn, "incident", f"{board}: the host wall cannot start sessions, so the"
                      f" board is paused and retries by itself ({why[:200]})", page=True,
                      fields=[("board", board), ("retry", retry)],
                      thread_key=run["task_id"], now=now)
    return since


def _wall_held(conn, run, run_json, task_id, review, now):
    """A session the host wall refuses for a reason that retrying never mends: a light-tier
    session (a config conflict) or one job's own fault (a workspace path too long). The card is
    held `capability` with the reason: no failure counted, no pause and no incident, so no Korin
    run is spent and other cards go on. A review role's run ends failed. Returns None."""
    why = ((run_json or {}).get("error") or host_wall.LIGHT_REFUSED)[:500]
    with txn(conn):
        blocks.block(conn, task_id, kind="capability", reason=why, run_id=run["run_id"], now=now,
                     run_pk=None if review else run["id"])
        if review:
            lease.end_run(conn, run["id"], status="failed", outcome="failed", error=why, now=now)


def retry(conn, cfg, run, why, now):
    """Reuses the review stage's own shape (`tick.Tick._review_one`'s
    `retry`/`review_retry`) for a non-review role whose invalid_output was purely a
    bad requests/ document, never a bad report - the same role runs once more, its
    next bundle carrying `why` (the validator's exact error, already naming the JSON
    path) as `invalid_document_error`, and this never touches `blocks.record_failure`
    (docs/ZENKAI.md Z2: a schema slip on a side document is not a second oracle on
    the work itself)."""
    task_id, role = run["task_id"], run["profile"]
    # the validator's own exact message (JSON path included), not `why`'s generic placeholder
    detail = reason(conn, run["id"]) or why
    prev_id = run["resumed_from"]
    prev = conn.execute("SELECT outcome FROM task_runs WHERE run_id=?", (prev_id,)).fetchone() if prev_id else None
    if prev is not None and prev["outcome"] == "invalid_document":
        blocks.block(conn, task_id, kind="invalid_document", reason=f"run {run['run_id']}: {detail}"[:2000],
                     run_id=run["run_id"], now=now)
        return None
    event(conn, task_id, "review_retry", {"role": role, "failed_run": run["run_id"], "error": detail[:500],
                                          "routed": "retry"}, run_pk=run["id"], run_id=run["run_id"], now=now)
    task = conn.execute("SELECT claim_lock, claim_expires, branch_name FROM tasks WHERE id=?",
                        (task_id,)).fetchone()
    meta = config.run_meta(cfg, task["branch_name"], invalid_document_error=detail)
    new_run_id = uuid.uuid4().hex
    run_pk = lease.start_run(conn, task_id, role=role, claim_lock=task["claim_lock"],
                             expires=task["claim_expires"], run_id=new_run_id, now=now, runner=run["runner"],
                             provider=run["provider"], account=run["account"], model=run["model"],
                             resumed_from=run["run_id"], metadata=meta)
    conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_pk, task_id))
    return new_run_id


def reason(conn, run_pk):
    """The `invalid_document` event's exact `reason` (the validator's error text, already naming
    the offending JSON path - spool.py `_mark_invalid`) for `run_pk`, or None (row 507(b): what a
    retried role's next bundle names as `invalid_document_error`)."""
    row = conn.execute("SELECT payload FROM task_events WHERE run_id=? AND kind='invalid_document'"
                       " ORDER BY id DESC LIMIT 1", (run_pk,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"] or "{}").get("reason")
    except ValueError:
        return None


def retry_error_kwargs(run):
    """{"invalid_document_error": <text>} when `run`'s own metadata carries one (a
    retry's restart run, set by `retry` above), else {} - spliced into
    `tick.Tick._launch`'s `inputs` dict so the retried role's bundle names the exact
    error (no `inputs` param reaches `_launch_unspawned`'s call, only the run row
    itself)."""
    try:
        err = json.loads(run["metadata"] or "{}").get("invalid_document_error")
    except (ValueError, AttributeError):
        err = None
    return {"invalid_document_error": err} if err else {}


def infra_cause(infra_roles, latest):
    """"invalid_document" when every still-infra review role's own miss was itself an
    invalid_document (row 507(b)), else None."""
    return ("invalid_document" if infra_roles and all(latest[r]["outcome"] == "invalid_document" for r in infra_roles)
           else None)
