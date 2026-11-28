#!/usr/bin/env python3
"""usage_guard.py - read the Claude usage window: usage event, snapshot and window
preflight. Its only pause is the weekly one board_bridge applies from the usage meter (D234)."""
import json, os, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import events  # noqa: E402

import verdicts  # noqa: E402  - ops_env() is the shared flag-reading idiom

HOME = Path.home()
STATE = events.state_root()
GUARD = STATE / "usage-guard.json"
# a snapshot of the session window's last known reading, for any reader that
# wants it without re-hitting the old system.
_REAL_USAGE_WINDOW_PATH = HOME / ".local" / "share" / "capsule" / "usage-window.json"
USAGE_WINDOW_PATH = _REAL_USAGE_WINDOW_PATH


def _snapshot_usage_window(util, reset, now):
    """Persist this pass's reading, override-or-refuse like `state_dir()` (refuses the
    untouched real default under `AGENT_OPS_NO_SEND`, so a test wanting it patches
    `USAGE_WINDOW_PATH` first), and never let a write failure raise out of `run()`."""
    path = USAGE_WINDOW_PATH
    if path == _REAL_USAGE_WINDOW_PATH and os.environ.get("AGENT_OPS_NO_SEND"):
        print("usage_guard: usage-window snapshot not written (default path refused under test)",
              file=sys.stderr)
        return
    try:
        persist_usage_window(util, reset, now=now, path=path)
    except Exception as exc:
        print(f"usage_guard: usage-window snapshot not written ({exc})", file=sys.stderr)


# agents share the 5-hour window with the owner; leave him the rest (his call, 2026-09-05)
PAUSE_AT = 0.85
# One-off override: OPS_PAUSE_AT in ops.env, a float in (0, 1]; invalid or absent falls
# back to PAUSE_AT above.


def pause_at(env=None):
    """Resolve the current preflight ceiling: OPS_PAUSE_AT over ops.env-under- os.environ
    (verdicts.ops_env()), the same idiom every C3 flag uses."""
    env = env if env is not None else verdicts.ops_env()
    raw = env.get("OPS_PAUSE_AT")
    if raw is None or str(raw).strip() == "":
        return PAUSE_AT
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return PAUSE_AT
    if not (0 < val <= 1):
        return PAUSE_AT
    return val

QUEUE_NAME = "window-queue.json"
# Lazily resolved (same 2026-09-11 incident class as budgets.BATCH/TURNS): `None` means
# "no override yet" - a test doing `mock.patch.object(usage_guard, "QUEUE", tmp)` still
# works exactly as before, since read_queue()/_write_queue()/queue_card() all prefer an
# explicit QUEUE over the default.
QUEUE = None
QUEUE_CAP = 50
# Row 121 changed M004: the estimate now, the `effort` input after the spec schema grows
# the field.
PREFLIGHT_FLOOR_PCT = 10.0
PREFLIGHT_MIN_S = 900.0
WINDOW_LENGTH_S = 5 * 3600  # the session window this module's docstring calls "the 5-hour window"


def preflight_enabled():
    """OPS_WINDOW_PREFLIGHT=1 in ops.env. Default off (row 121)."""
    return os.environ.get("OPS_WINDOW_PREFLIGHT") == "1"


def _floors():
    def f(key, default):
        try:
            return float(os.environ.get(key) or default)
        except ValueError:
            return default
    return (f("OPS_WINDOW_PREFLIGHT_FLOOR_PCT", PREFLIGHT_FLOOR_PCT),
            f("OPS_WINDOW_PREFLIGHT_MIN_S", PREFLIGHT_MIN_S))


def window_estimate(util, reset, now=None):
    """Headroom to the preflight ceiling (pause_at(): PAUSE_AT by default,
    OPS_PAUSE_AT for a one-off day) and seconds until the session window resets."""
    now = time.time() if now is None else now
    ceiling = pause_at()
    return {"headroom_pct": None if util is None else round((ceiling - util) * 100, 1),
            "reset_in_s": None if reset is None else round(float(reset) - now, 1)}


def _refusal(headroom_pct, reset_in_s):
    """The refusal reason always carries the headroom
    as a percentage - `window: <p>% headroom, resets in <s>s` - so T6's report line
    can parse it out of the queue regardless of which floor tripped."""
    reset_txt = f"{int(reset_in_s)}s" if reset_in_s is not None else "unknown"
    return f"window: {headroom_pct}% headroom, resets in {reset_txt}"


def preflight(util, reset, now=None, floor_pct=None, min_s=None):
    """(ok, reason). Not ok means the dispatcher queues the card SILENTLY -
    no DECISION, no Telegram, no block (row 121). Unknown is always ok: a
    missing signal must never stop work."""
    fp, ms = _floors()
    floor_pct = fp if floor_pct is None else float(floor_pct)
    min_s = ms if min_s is None else float(min_s)
    est = window_estimate(util, reset, now)
    if est["headroom_pct"] is None:
        return True, "no window signal"
    if est["headroom_pct"] < floor_pct:
        return False, _refusal(est["headroom_pct"], est["reset_in_s"])
    if est["reset_in_s"] is not None and 0 < est["reset_in_s"] < min_s:
        return False, _refusal(est["headroom_pct"], est["reset_in_s"])
    return True, f"headroom {est['headroom_pct']}%"


def state_dir(create=False):
    """Same override-or-refuse rule as budgets.state_dir() / verdicts."""
    override = os.environ.get("OPS_USAGE_GUARD_STATE")
    if not override and os.environ.get("AGENT_OPS_NO_SEND"):
        raise RuntimeError("refusing to touch the default usage-guard state under test: "
                            "set OPS_USAGE_GUARD_STATE explicitly")
    p = Path(override) if override else STATE
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def _queue_path(explicit=None):
    if explicit is not None:
        return Path(explicit)
    if QUEUE is not None:
        return Path(QUEUE)
    return state_dir(create=True) / QUEUE_NAME


def read_queue(path=None):
    try:
        rows = json.loads(_queue_path(path).read_text())
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _prune_stale(rows, reset_at):
    """Drop rows queued in an earlier, already-reset session window."""
    if reset_at is None:
        return rows
    cutoff = float(reset_at) - WINDOW_LENGTH_S
    return [r for r in rows if r.get("ts", 0) >= cutoff]


def _write_queue(rows, path=None):
    p = _queue_path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows))
        return True
    except Exception as exc:
        print(f"usage_guard: window queue not written ({exc})", file=sys.stderr)
        return False


def drain_queue(now=None, reset_at=None, path=None):
    """Prune every stale row (see `_prune_stale`) and persist the result."""
    rows = _prune_stale(read_queue(path), reset_at)
    _write_queue(rows, path)
    return rows


def queue_card(board_name, task_id, reason, now=None, path=None, reset_at=None):
    """Record one silently-queued card and return the queue length."""
    p = _queue_path(path)
    rows = _prune_stale(read_queue(p), reset_at)
    key = (str(board_name), str(task_id))
    rows = [r for r in rows if (r.get("board"), r.get("task_id")) != key]
    rows.append({"board": key[0], "task_id": key[1],
                 "reason": str(reason)[:120], "ts": float(time.time() if now is None else now)})
    rows = rows[-QUEUE_CAP:]
    if not _write_queue(rows, p):
        return 0
    return len(rows)


class UsageWindow(tuple):
    """(utilization 0..1 or None, reset_epoch or None, week_pct or None) - exactly the
    3-tuple every caller today (grep confirms: board_bridge.py, the test suite's
    `return_value=(...)` mocks) already unpacks or plain-tuple-mocks, so this stays a
    drop-in tuple."""
    def __new__(cls, util, reset, week_pct, weekly):
        self = super().__new__(cls, (util, reset, week_pct))
        self.weekly = weekly
        return self


def usage_window():
    """(utilization 0..1 or None, reset_epoch or None, week_pct or None). Only the weekly slot
    has a source: the meter over local Claude Code transcripts (D234, capsule/usage_meter.py),
    `week_pct` a percent 0..100. The 5-hour utilization and reset stay None, so `preflight`
    fails open. `.weekly` is [("Current week", fraction, reset_epoch)]; `.meter` is the raw dict."""
    from capsule import usage_meter     # lazy: a bare copy of maintenance/ has no sibling capsule/
    m = usage_meter.weekly_usage(state_dir=STATE, now=time.time())
    uw = UsageWindow(None, None, None if m is None else m["fraction"] * 100,
                     [] if m is None else [("Current week", m["fraction"], m["reset_at"])])
    uw.meter = m
    return uw


def weekly_block(meter):
    """The silent-queue reason once the weekly meter dict is at or over its pause threshold
    (`usage_pause_at`, default 0.90), else None. No reading is never a block."""
    if meter and meter["fraction"] >= meter["pause_at"]:
        return f"weekly: {meter['fraction'] * 100:.0f}% used of the weekly limit"
    return None


def _iso(epoch):
    """epoch seconds -> ISO 8601 UTC string, or None for None."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def persist_usage_window(util, reset, now=None, *, path):
    """Atomically write the session window's last reading to `path`: {"utilization":
    0.0-1.0, "reset_at": <iso|null>, "written_at": <iso>, "source": "usage_guard"}."""
    if util is None:
        return None
    now = time.time() if now is None else now
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"utilization": util, "reset_at": _iso(reset), "written_at": _iso(now),
           "source": "usage_guard"}
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


def read_usage_window(path, max_age_s=900, now=None):
    """Read back a `persist_usage_window` snapshot, or None when the file is missing,
    stale (older than `max_age_s`), or malformed (bad JSON, wrong shape,
    `utilization` not a 0.0-1.0 number)."""
    now = time.time() if now is None else now
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    util = doc.get("utilization")
    written_at = doc.get("written_at")
    reset_at = doc.get("reset_at")
    source = doc.get("source")
    if isinstance(util, bool) or not isinstance(util, (int, float)):
        return None
    if not (0.0 <= util <= 1.0):
        return None
    if not isinstance(written_at, str):
        return None
    try:
        wa = datetime.fromisoformat(written_at)
    except ValueError:
        return None
    if wa.tzinfo is None:
        wa = wa.replace(tzinfo=timezone.utc)
    if now - wa.timestamp() > max_age_s:
        return None
    if reset_at is not None and not isinstance(reset_at, str):
        return None
    if source is not None and not isinstance(source, str):
        return None
    return doc


def load_guard():
    try:
        return json.loads(GUARD.read_text())
    except Exception:
        return {"paused_until": None}


def run(check_only=False):
    now = time.time()
    uw = usage_window()
    util, reset, week_pct = uw[0], uw[1], uw[2]
    meter = getattr(uw, "meter", None)
    events.log("usage", "usage_guard", f"session {util} reset {reset}", payload={
        "session_pct": None if util is None else round(util * 100, 1),
        "week_pct": None if week_pct is None else round(week_pct, 1),
        "session_reset": reset, "weekly_paused": weekly_block(meter) is not None})
    if check_only:
        week_line = f"week={None if week_pct is None else round(week_pct, 1)}"
        if meter:
            week_line += f" week_used={round(meter['used'])} week_limit={round(meter['limit'])}"
        reset_at = time.strftime("%H:%M", time.localtime(reset)) if reset else None
        print(f"util={util} {week_line} reset={reset_at}")
        return 0
    _snapshot_usage_window(util, reset, now)
    print("usage_guard: ok")
    return 0


if __name__ == "__main__":
    sys.exit(run(check_only="--check" in sys.argv))
