"""Weekly usage pause (D234): the tick starts no new card runs while the usage meter is at or
past its pause line (`usage_pause_at`, default 0.90). Runs in flight, reviews, rulings and
landings go on.

`held` reads the meter once per pass. A meter that says None (off, not calibrated, any error)
never holds: fail open. One `weekly_pause` event when a hold starts and one `weekly_resume` when it
ends, on the sentinel card id `weekly`; the guard `weekly_pause` remembers which side the last pass
was on, so a quiet pass writes nothing. Never raises."""
import sys

from capsule import db, usage_meter
from capsule.db import guard_get, guard_set

GUARD = "weekly_pause"
TASK = "weekly"      # board-level events carry a sentinel card id, like eval_check's "eval"


def meter(home, now, state_dir):
    """The usage meter's answer for this host (a test swaps this), or None."""
    return usage_meter.weekly_usage(state_dir=state_dir, now=now, home=home)


def held(conn, home, now, state_dir=None):
    """True while new card runs must wait; records the pause/resume transition once."""
    try:
        return _held(conn, home, now, state_dir)
    except Exception as exc:
        print(f"capsule tick: weekly pause check failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return False


def _held(conn, home, now, state_dir):
    try:
        got = meter(home, now, state_dir)
    except Exception as exc:       # fail open, and a stale hold must lift too
        print(f"capsule tick: weekly pause check failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        got = None
    over = got is not None and got["fraction"] >= got["pause_at"]
    was = bool((guard_get(conn, GUARD)[0] or {}).get("held"))
    if over != was:
        doc = got or {}
        pct = round(100 * doc["fraction"]) if "fraction" in doc else None
        with db.txn(conn):        # the event and the flip land together or not at all
            at = doc.get("reset_at")
            if over:
                db.event(conn, TASK, "weekly_pause", {"pct": pct, "reset_at": at}, now=now)
            else:
                db.event(conn, TASK, "weekly_resume", {"pct": pct, "reset_at": at}, now=now)
            guard_set(conn, GUARD, {"held": over}, now=now)
    return over
