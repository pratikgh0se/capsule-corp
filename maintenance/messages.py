#!/usr/bin/env python3
"""messages.py - how many messages Capsule may send the owner in a day."""
import datetime as _dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import events  # noqa: E402

MAINT = Path(__file__).resolve().parent
STATE = events.state_root()
_DEFAULT_COUNTS = STATE / "message-budget.json"
COUNTS = _DEFAULT_COUNTS

SCHEDULED_MAX = 2      # the brief and the close - row 136
ACTIONABLE_MAX = 8     # a ninth actionable item waits for tomorrow's brief (D209: 2+8=10/day)
REFIT_DATE = "2026-09-24"
KINDS = ("scheduled", "actionable", "page")
FLAG = "OPS_BRIEF_V2"
_ENV = {"scheduled": ("OPS_SCHEDULED_MAX", SCHEDULED_MAX),
        "actionable": ("OPS_ACTIONABLE_MAX", ACTIONABLE_MAX)}
def enabled():
    """OPS_BRIEF_V2, read at call time (run.sh exports it into every job)."""
    return str(os.environ.get(FLAG, "0")).strip() == "1"


def _limit(kind):
    name, default = _ENV.get(kind, ("", 0))
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:      # a malformed override falls back to the constant,
        return default      # the usage_guard._floors discipline


def _explicit_path(path=None):
    """An explicit choice of state path, or None when the caller is relying on the
    frozen default: an explicit `path=`, OPS_MESSAGE_STATE, or a test having rebound
    `COUNTS` away from its import-time default all count as explicit (mirrors
    verdicts.py:verdict_dir())."""
    if path is not None:
        return Path(path)
    override = os.environ.get("OPS_MESSAGE_STATE")
    if override:
        return Path(override)
    if COUNTS != _DEFAULT_COUNTS:
        return Path(COUNTS)
    return None


def _resolve_read_path(path=None):
    """Reads never raise (the brief's fail-open rule: a block that cannot be computed is
    absent, never a broken job) - an unoverridden default under test just reads as
    the blank/zero day, since nothing was ever written to it there."""
    explicit = _explicit_path(path)
    return explicit if explicit is not None else Path(COUNTS)


def _resolve_write_path(path=None):
    """The override-or-refuse rule (mirrors verdicts.py:verdict_dir()): only an
    untouched, unoverridden default refuses to be WRITTEN under AGENT_OPS_NO_SEND -
    falling through to the real state file in a test run is always a bug."""
    explicit = _explicit_path(path)
    if explicit is not None:
        return explicit
    if os.environ.get("AGENT_OPS_NO_SEND"):
        raise RuntimeError("refusing to touch the default message-budget state "
                            "under test: set OPS_MESSAGE_STATE or pass path=")
    return Path(COUNTS)


def _day(now=None):
    now = _dt.datetime.now().astimezone() if now is None else now
    return now.date().isoformat()


def _blank(day, carry=0):
    d = {"day": day, "deferred": 0, "carry": max(0, int(carry or 0)), "deferred_keys": []}
    d.update({k: 0 for k in KINDS})
    return d


def _item_key(text, board=None, task_id=None):
    """What identifies "the same item" for one day's dedupe."""
    if board and task_id:
        return f"{board}/{task_id}"
    import hashlib
    return "h:" + hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:16]


def _keys(stored):
    ks = stored.get("deferred_keys")
    if not isinstance(ks, list):
        return []
    return [str(k) for k in ks if isinstance(k, str)]


def _int(d, key):
    try:
        return max(0, int(d.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def counts(now=None, path=None):
    """Today's counters. A stored file from another day rolls over: the counts reset and
    its `deferred` becomes today's `carry` - a ONE-DAY window, so the 08:30 brief can
    still report what yesterday (and only yesterday) deferred; a prior `carry` does
    not itself roll forward again, or a deferral would be reported as "waiting"
    forever after C2's batch has long since printed and pruned it."""
    day = _day(now)
    p = _resolve_read_path(path)
    try:
        stored = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = None
    if not isinstance(stored, dict):
        return _blank(day)
    if stored.get("day") != day:
        return _blank(day, carry=_int(stored, "deferred"))
    out = _blank(day, carry=_int(stored, "carry"))
    for k in list(KINDS) + ["deferred"]:
        out[k] = _int(stored, k)
    out["deferred_keys"] = _keys(stored)
    return out


def _write(counters, path=None):
    p = _resolve_write_path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(counters, indent=1, sort_keys=True), encoding="utf-8")
        return True
    except OSError:
        return False


def allow(kind, *, now=None, path=None):
    """(may_send, why). A page is ALWAYS allowed: the budget reports pages, it
    never withholds one. Any read failure is permissive."""
    if kind == "page":
        return True, "pages are never withheld"
    c = counts(now=now, path=path)
    n, lim = c.get(kind, 0), _limit(kind)
    why = f"{n}/{lim} {kind} sent today"
    return (n < lim), why


def record(kind, *, now=None, path=None):
    """Count one sent message. Returns the day's counters."""
    c = counts(now=now, path=path)
    if kind in KINDS:
        c[kind] = c.get(kind, 0) + 1
    _write(c, path)
    return c


def defer_actionable(text, *, board=None, task_id=None, now=None, path=None, batch_path=None):
    """M186: the item that did not fit waits for the next brief."""
    c = counts(now=now, path=path)
    key = _item_key(text, board, task_id)
    keys = c.get("deferred_keys") or []
    if key in keys:
        return c["deferred"], False
    c["deferred_keys"] = keys + [key]
    c["deferred"] = c.get("deferred", 0) + 1
    _write(c, path)
    try:
        import budgets
        budgets.defer("waiting", str(text)[:160], board=board, task_id=task_id,
                      path=batch_path)
    except Exception:       # the counter is the contract; the batch is a courtesy
        pass
    return c["deferred"], True


def budget_line(now=None, path=None):
    """The brief's `budget:` line - present only when something was actually
    deferred (the power_lines()/retrieval_lines() discipline)."""
    c = counts(now=now, path=path)
    waiting = c.get("deferred", 0) + c.get("carry", 0)
    if not waiting:
        return None
    return (f"budget: {waiting} actionable item(s) waiting - the cap is "
            f"{_limit('actionable')}/day (re-fit {REFIT_DATE})")


