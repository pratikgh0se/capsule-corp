"""The weekly rhythm as one calendar (COM-11, card R1; rows 136, 450, 600, 629).

`_posture(cfg, local_dt)` says what the week allows at one local wall-clock time: may the tick
dispatch, may a product card land, may a harness card land. OFF unless a board sets
`rhythm.enabled: true`. Nothing in the factory consults it yet (later cards add the holds), so
the only caller is `python3 -m capsule rhythm B --at YYYY-MM-DDTHH:MM` (`main`), read-only.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from capsule import paths

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULTS = {"enabled": False, "tz": "Asia/Bangkok", "saturday_zero": True,
            "wed_epic_pause": ["13:00", "17:00"], "fri_freeze": "16:00",
            "freeze_until": "Sun 18:00"}
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DAY_HHMM = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([01]\d|2[0-3]):([0-5]\d)$")
_AT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d$")
OFF = "rhythm off"
DAY = 1440
SUNDAY_REVIEW_END = 6 * DAY + 18 * 60     # Sun 18:00: the fixed end of the Sunday review


def _bad(key, why):
    return ValueError(f"rhythm.{key}: {why}")


def _hhmm(key, text):
    m = _HHMM.match(text) if isinstance(text, str) else None
    if not m:
        raise _bad(key, "must be HH:MM (24 h)")
    return int(m[1]) * 60 + int(m[2])


def _week_minute(key, text):
    """'Sun 18:00' -> minutes since Monday 00:00."""
    m = _DAY_HHMM.match(text) if isinstance(text, str) else None
    if not m:
        raise _bad(key, "must be 'Day HH:MM' inside the week, like 'Sun 18:00' (it cannot run"
                        " past Sunday)")
    return DAYS.index(m[1]) * DAY + int(m[2]) * 60 + int(m[3])


def parse(block):
    """Validate a `rhythm` block (None or {} is valid) and return it with defaults filled."""
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise ValueError("rhythm: not an object")
    extra = sorted(set(block) - set(DEFAULTS))
    if extra:
        raise _bad(extra[0], "unknown key")
    cfg = {**DEFAULTS, **block}
    for key in ("enabled", "saturday_zero"):
        if not isinstance(cfg[key], bool):
            raise _bad(key, "must be a bool")
    try:
        ZoneInfo(cfg["tz"])
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        raise _bad("tz", "not a known IANA zone") from None
    pause = cfg["wed_epic_pause"]
    if not isinstance(pause, list) or len(pause) != 2:
        raise _bad("wed_epic_pause", "must be two HH:MM times [start, end]")
    if _hhmm("wed_epic_pause", pause[0]) >= _hhmm("wed_epic_pause", pause[1]):
        raise _bad("wed_epic_pause", "start must be before end")
    if _week_minute("freeze_until", cfg["freeze_until"]) <= 4 * DAY + _hhmm(
            "fri_freeze", cfg["fri_freeze"]):
        raise _bad("freeze_until", "must be after fri_freeze")
    return cfg


def _posture(cfg, local_dt):
    """The posture at `local_dt` for a parsed `cfg`. Both landing flags are always given (the
    caller picks `land_harness` for the `capsule-corp` board, else `land_product`)."""
    if local_dt.tzinfo is not None:
        local_dt = local_dt.astimezone(ZoneInfo(cfg["tz"]))
    day = local_dt.weekday()
    now = day * DAY + local_dt.hour * 60 + local_dt.minute
    out = {"day": DAYS[day], "dispatch": True, "land_product": True, "land_harness": True,
           "reason": "default" if cfg["enabled"] else OFF, "tz": cfg["tz"]}
    if not cfg["enabled"]:
        return out
    start, end = (_hhmm("wed_epic_pause", t) for t in cfg["wed_epic_pause"])
    freeze = (4 * DAY + _hhmm("fri_freeze", cfg["fri_freeze"]),
              _week_minute("freeze_until", cfg["freeze_until"]))
    # (rule in force, does it hold, what it turns off); the first rule that holds names the reason
    rules = (
        ("saturday zero", cfg["saturday_zero"] and 5 * DAY <= now < 6 * DAY,
         ("dispatch", "land_product", "land_harness")),
        ("sunday review", 6 * DAY <= now < SUNDAY_REVIEW_END,
         ("dispatch", "land_product", "land_harness")),
        ("friday freeze", freeze[0] <= now < freeze[1], ("land_product",)),
        ("wednesday epic pause", 2 * DAY + start <= now < 2 * DAY + end, ("dispatch",)),
    )
    for name, holds, off in rules:
        if holds:
            out.update({k: False for k in off})
            if out["reason"] == "default":
                out["reason"] = name
    return out


def _at(text):
    try:
        if not _AT.match(text):
            raise ValueError(text)
        return datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not YYYY-MM-DDTHH:MM") from None


def add_parser(sub):
    p = sub.add_parser("rhythm", help="the weekly rhythm calendar for a board at a time"
                                      " (COM-11; read-only, off unless rhythm.enabled)")
    p.add_argument("board")
    p.add_argument("--at", required=True, type=_at, help="local wall clock, YYYY-MM-DDTHH:MM")
    p.add_argument("--capsule-home", default=None)


def main(args, parser=None):
    """Print the posture JSON for the board. Reads `boards/B/config.json`; writes nothing."""
    home = paths.home(args.capsule_home)
    problem = paths.board_name_problem(args.board)
    if problem:
        print(f"capsule rhythm: {problem}", file=sys.stderr)
        return 1
    if not paths.board_dir(args.board, home).is_dir():
        print(f"capsule rhythm: no board {args.board!r} under {home / 'boards'}", file=sys.stderr)
        return 1
    path = paths.config_path(home, args.board)
    try:
        cfg = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(cfg, dict):
            raise ValueError("config.json is not a JSON object")
        print(json.dumps(_posture(parse(cfg.get("rhythm")), args.at)))
    except (OSError, ValueError) as exc:     # bad JSON and a bad rhythm block are both ValueError
        print(f"capsule rhythm: {path}: {exc}", file=sys.stderr)
        return 1
    return 0
