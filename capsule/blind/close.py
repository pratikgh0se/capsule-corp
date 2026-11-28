"""Closing a blind-tester run (blind spec 3.1; R30): `close` validates `blind_tests.v1` (every
numbered acceptance statement is covered or explained) and records it on the run and in a
`blind_tests_reported` event, or ends the run failed. Split from `dispatch` for its size."""
import json

from capsule import blocks
from capsule import invalid_doc as idoc
from capsule import lease
from capsule.blind.dispatch import ROLE
from capsule.db import event, txn
from capsule.runners import host_wall


def _inputs(sp):
    """The `inputs` of the run's job.json ({} when unreadable)."""
    try:
        return json.loads((sp / "job.json").read_text())["inputs"]
    except (OSError, ValueError, KeyError):
        return {}


def _unaccounted(doc, count):
    """Why `doc` leaves the card's `count` numbered statements unaccounted for, or "": every
    statement is covered by a test or not testable for a reason, none both, none out of range."""
    covered = {c["statement"] for c in doc["covered"]}
    explained = {n["statement"] for n in doc["not_testable"]}
    problems = (
        (f"statements outside 1..{count}", sorted(s for s in covered | explained
                                                  if not 1 <= s <= count)),
        ("statements with neither a test nor a reason",
         sorted(set(range(1, count + 1)) - covered - explained)),
        ("statements both covered and not testable", sorted(covered & explained)))
    return "; ".join(f"{what} {nums}" for what, nums in problems if nums)


def _record(conn, run, doc, now):
    """The valid report on the run, and the event that keeps what was not tested and why."""
    with txn(conn):
        conn.execute("UPDATE task_runs SET handoff=? WHERE id=?",
                     (json.dumps(doc, ensure_ascii=False), run["id"]))
        covered, untested = len(doc["covered"]), len(doc["not_testable"])
        lease.end_run(conn, run["id"], status="done", outcome="completed", now=now,
                      summary=f"{covered} covered, {untested} not testable")
        event(conn, run["task_id"], "blind_tests_reported",
              {"covered": doc["covered"], "not_testable": doc["not_testable"],
               "question": doc.get("question", "")},
              run_pk=run["id"], run_id=run["run_id"], now=now)


def _fail(conn, cfg, run, run_json, why, now):
    """End the run failed through the reviewers' own path (`close_failed_run`: a crash, no run.json
    or a `fatal` class, also writes the typed `crashed` event the incident opens from), then say so.
    The card is a review-shaped run's subject, never charged."""
    idoc.close_failed_run(conn, cfg, run, run_json, run["error_class"], ROLE, run["task_id"],
                          True, why, False, now)
    with txn(conn):
        event(conn, run["task_id"], "blind_tester_failed", {"reason": why[:500]},
              run_pk=run["id"], run_id=run["run_id"], now=now)


def close(tick, conn, cfg, run, sp, run_json, now, counts):
    """Close a finished tester run (the signature of `Tick._close`; `counts` is unused). A valid,
    complete `blind_tests.v1` is recorded on the run and in a `blind_tests_reported` event, with
    the statements that could not be tested and why; anything else ends the run `failed` with a
    `blind_tester_failed` event. The card is never touched."""
    done = run_json or {}
    tokens = done.get("tokens") or {}
    with txn(conn):
        conn.execute(
            "UPDATE task_runs SET tokens_in=?, tokens_out=?, tokens_cache_read=?,"
            " tokens_cache_write=?, attempts=?, error_class=COALESCE(error_class,?) WHERE id=?",
            (*(tokens.get(k) for k in ("in", "out", "cache_read", "cache_write")),
             done.get("attempts"), done.get("error_class") or (None if run_json else "fatal"),
             run["id"]))
    run = tick._run(conn, run["id"])
    stored = done.get("error_class")      # an earlier `invalid_output` hides the stored pause class
    if run["error_class"] in blocks.PAUSE_CLASSES or (
            run["error_class"] == "invalid_output" and stored in blocks.PAUSE_CLASSES):
        until = tick._pause(conn, run, done.get("retry_after"), now, run_json)
        with txn(conn):             # the mark and the release land together
            tick._release_rate_limited(conn, run, True, until, now)
            if stored == host_wall.TIMEOUT_CLASS:    # its pause scope is empty by design: the
                conn.execute(                        # retry is at once, so `dispatch._known`
                    "UPDATE task_runs SET metadata=json_set(COALESCE(metadata, '{}'),"
                    " '$.unpaused_release', 1) WHERE id=?", (run["id"],))   # counts the run
        return None
    doc, why = ((None, f"error_class {run['error_class']}") if run["error_class"]
                else tick._report(sp, run, conn, now))
    count = len(_inputs(sp).get("card", {}).get("acceptance", []))
    if doc is not None and count and (why := _unaccounted(doc, count)):
        doc = None
    if doc is None:
        return _fail(conn, cfg, run, run_json, why, now)
    return _record(conn, run, doc, now)
