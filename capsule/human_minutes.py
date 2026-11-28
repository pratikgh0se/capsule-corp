"""EVAL-10: The owner's time, counted from the actions the board already records (his ruling,
2026-10-23). Each `capsule human` file is a fixed number of minutes by action type. The
minutes are ESTIMATES, not measurements; nothing else about him is tracked."""
import json
import math
import time

from capsule import human, paths, report

# Estimated minutes per action; override under `human_minutes` in the board config.json.
DEFAULTS = {"approve": 2, "close": 1, "reset": 2, "answer": 3, "command": 2}
# human-file `kind` -> action type; any other recorded kind is a manual command.
TYPES = {"approve": "approve", "accept": "approve", "close": "close", "reset": "reset",
         "unblock": "reset", "answer": "answer", "screen-ok": "answer", "screen-fix": "answer"}
DECISIONS = ("approve", "close", "answer")   # the types that count as a decision


def measure(home, board, docs, since_ts, now=None):
    """Minutes/day, decisions/day and the delegation share from human-file `docs` (dicts).
    Author `cos` is the chief of staff: counted for delegation, never as the owner's time; a file
    with no author is the owner's. The window is `since` to `now`, or first action to `now`."""
    path = paths.resolve_config(home, board)   # `human_minutes` in it overrides DEFAULTS
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}   # no config file: defaults; a malformed one raises ValueError
    block = cfg.get("human_minutes") or {} if isinstance(cfg, dict) else None
    if not isinstance(block, dict) or any(
            k not in DEFAULTS or isinstance(v, bool) or not isinstance(v, (int, float))
            or not math.isfinite(v) or v < 0 for k, v in block.items()):
        raise ValueError(f"human_minutes: keys from {sorted(DEFAULTS)} with finite minutes >= 0")
    w = {**DEFAULTS, **block}
    now = int(now if now is not None else time.time())
    cos = sum(1 for d in docs if human.is_cos(d.get("author")))
    mine = [d for d in docs
            if not human.is_cos(d.get("author")) and d.get("tag") not in report.EXCLUDED_TAGS]
    kinds = [TYPES.get(d.get("kind"), "command") for d in mine]
    start = since_ts or min((int(d.get("at") or now) for d in mine), default=now)
    days = max((now - start) / 86400, 1.0)
    minutes = sum(w[k] for k in kinds)
    decisions = sum(1 for k in kinds if k in DECISIONS)
    return {
        "human_minutes": minutes, "human_minutes_per_day": minutes / days,
        "decisions_per_day": decisions / days, "human_window_days": days,
        "human_minutes_weights": w,
        "human_minutes_note": "estimates: a fixed minutes-per-action weight, not a measurement",
        "owner_actions": len(mine), "cos_actions": cos,
        "delegation_ratio": cos / (cos + len(mine)) if cos + len(mine) else "unmeasured",
        "delegation_note": "cos share of cos+owner human actions; factory actions not counted",
    }
