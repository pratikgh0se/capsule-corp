"""Weekly usage meter (D234): an estimate of how much of the weekly limit is spent, summed from
the token `usage` fields of the Claude Code transcripts under `~/.claude/projects/**/*.jsonl`.

Privacy: a line is parsed, its ids, model, timestamp and four numbers kept, the rest dropped.
No message text is stored, returned or logged. Every failure returns None (no signal, never a
pause) with one warning per process.

Weight of a message = model_weight * (input + 5 * output + 1.25 * cache_write + 0.1 * cache_read).
The token ratios follow the public price ratios; the model weights are guesses. Every weight is
a guess that the one-off limit calibration absorbs. The weekly pause that reads the meter:
docs/architecture/components/dispatch-and-caps.md.

State `<state_dir>/usage-meter.json` holds per-file read offsets, running sums per model and a
bounded table `recent` of messages whose record time is within RECENT_S of the scan. One message
is written several times within seconds, so a later copy is found in `recent` and only its
per-field growth is added. A copy that straddles more than RECENT_S cannot happen in practice.
"""
import json
import os
import sys
import tempfile
from itertools import takewhile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from capsule import paths

FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
RECENT_S = 2 * 3600
PAUSE_AT = 0.90    # weekly pause threshold (D234); `usage_pause_at` in the host config
STATE_NAME = "usage-meter.json"
RESET = {"dow": 6, "hour": 4, "tz": "Asia/Bangkok"}    # Sunday 04:00 (dow 0 = Monday)
WEIGHTS = {"output": 5.0, "cache_write": 1.25, "cache_read": 0.1,
           "models": {"haiku": 0.2, "sonnet": 1.0, "opus": 1.0, "fable": 2.0}}
_warned = []


def _warn(why):
    if not _warned:
        _warned.append(why)
        print(f"usage_meter: {why}", file=sys.stderr)


def _week_start(now, reset):
    """Epoch of the most recent weekly reset at or before `now`."""
    local = datetime.fromtimestamp(now, ZoneInfo(reset["tz"]))
    at = local.replace(hour=reset["hour"], minute=0, second=0, microsecond=0)
    at -= timedelta(days=(local.weekday() - reset["dow"]) % 7)
    if at > local:
        at -= timedelta(days=7)
    return at.timestamp()


def _epoch(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _add(state, line, start):
    """Fold one transcript line into the state; lines that are not usage records are ignored."""
    if b'"usage"' not in line or b'"assistant"' not in line:
        return
    try:
        doc = json.loads(line)
        msg, usage = doc["message"], doc["message"]["usage"]
        key = f"{msg.get('id') or doc['uuid']}|{doc.get('requestId')}"
        ts = _epoch(doc.get("timestamp"))
        got = [int(usage.get(f) or 0) for f in FIELDS]
    except Exception:       # one odd line must never stall the meter
        return
    model = str(msg.get("model") or "?")
    if ts is None or ts < start or model == "<synthetic>":
        return
    old = state["recent"].get(key, [0, model, 0, 0, 0, 0])[2:]
    delta = [max(g - o, 0) for g, o in zip(got, old)]
    state["recent"][key] = [ts, model] + [max(g, o) for g, o in zip(got, old)]
    sums = state["sums"].setdefault(model, [0, 0, 0, 0])
    for i, d in enumerate(delta):
        sums[i] += d


def _read(path, known, state, start):
    """Fold the complete lines past `known["offset"]` into the state; a torn last line waits."""
    try:
        with open(path, "rb") as fh:
            fh.seek(known["offset"])
            for line in takewhile(lambda ln: ln.endswith(b"\n"), fh):
                known["offset"] += len(line)
                _add(state, line, start)
    except OSError:
        _warn(f"transcript unreadable: {path.name}")


def _scan(root, state, start):
    """Read each transcript from its saved offset; False when a file shrank (state is stale)."""
    for path in sorted(Path(root).rglob("*.jsonl")):
        try:
            st = path.stat()
        except OSError:
            continue
        known = state["files"].setdefault(str(path), {"offset": 0, "inode": st.st_ino})
        if st.st_size < known["offset"] or known["inode"] != st.st_ino:
            return False
        if st.st_mtime >= start and st.st_size > known["offset"]:
            _read(path, known, state, start)
    return True


def _fresh(start):
    return {"week_start": start, "files": {}, "recent": {}, "sums": {}}


def _nums(row, n):
    return (isinstance(row, list) and len(row) == n
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in row))


def _inner_ok(state):
    """Recent rows are [ts, model, 4 numbers], sums rows 4 numbers, file rows {offset, inode}."""
    return (all(isinstance(r, list) and len(r) == 6 and isinstance(r[1], str)
                and _nums(r[:1] + r[2:], 5) for r in state["recent"].values())
            and all(_nums(r, 4) for r in state["sums"].values())
            and all(isinstance(r, dict) and isinstance(r.get("offset"), int)
                    and "inode" in r for r in state["files"].values()))


def _load(path, start):
    """The saved state for this week, or a fresh one when missing, corrupt or the wrong shape."""
    try:
        state = json.loads(Path(path).read_text("utf-8"))
        ok = (state["week_start"] == start
              and all(isinstance(state[k], dict) for k in ("files", "recent", "sums")))
        return state if ok and _inner_ok(state) else _fresh(start)
    except (OSError, ValueError, KeyError, TypeError):
        return _fresh(start)


def _save(path, state, now):
    state["recent"] = {k: v for k, v in state["recent"].items() if v[0] >= now - RECENT_S}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def _units(sums, weights):
    """Weighted token units: `sums` is {model: [input, output, cache_write, cache_read]}."""
    total = 0.0
    for model, (i, o, cw, cr) in sums.items():
        w = next((v for k, v in weights["models"].items() if k in model.lower()), 1.0)
        total += w * (i + weights["output"] * o + weights["cache_write"] * cw
                      + weights["cache_read"] * cr)
    return total


def _run(root, path, start):
    """Scan into the saved state; a state that raises or went stale is rebuilt, rescanned once."""
    state = _load(path, start)
    try:
        ok = _scan(root, state, start)
    except Exception:
        ok = False
    if not ok:
        state = _fresh(start)
        _scan(root, state, start)
    return state


def _meter(state_dir, now, home):
    cfg = json.loads(paths.config_path(home).read_text("utf-8"))
    limit = cfg.get("usage_weekly_limit")
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit <= 0:
        return _warn("usage_weekly_limit not set in the host config: weekly meter off")
    reset = {**RESET, **(cfg.get("usage_weekly_reset") or {})}
    root = Path(cfg.get("usage_transcripts_root") or Path.home() / ".claude" / "projects")
    if not root.is_dir():
        return _warn(f"transcripts directory missing: {root}: weekly meter off")
    start, path = _week_start(now, reset), Path(state_dir) / STATE_NAME
    state = _run(root, path, start)
    _save(path, state, now)
    wts = cfg.get("usage_weights") or {}
    wts = {**WEIGHTS, **wts, "models": {**WEIGHTS["models"], **(wts.get("models") or {})}}
    used = _units(state["sums"], wts)
    stop = cfg.get("usage_pause_at")
    ok = isinstance(stop, (int, float)) and not isinstance(stop, bool) and 0 < stop <= 1
    return {"fraction": used / limit, "used": used, "limit": float(limit), "week_start": start,
            "reset_at": start + 7 * 86400, "pause_at": float(stop) if ok else PAUSE_AT}


def weekly_usage(*, state_dir, now, home=None):
    """{"fraction", "used", "limit", "week_start", "reset_at", "pause_at"} for the current week,
    or None (no signal). Needs `usage_weekly_limit` (units) in the host config
    `<home>/config.json`; optional `usage_pause_at` (float), `usage_weekly_reset` {dow, hour,
    tz}, `usage_weights` and `usage_transcripts_root`. Never raises: a
    broken meter must never stop work."""
    try:
        return _meter(Path(state_dir), now, None if home is None else Path(home))
    except Exception as exc:
        _warn(f"meter unavailable ({type(exc).__name__}: {exc})")
