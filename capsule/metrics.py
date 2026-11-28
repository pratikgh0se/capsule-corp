"""Row 470: a dead-man alert for unblocked, unowned work."""
import contextlib
import json
import os
import sys
import time
from pathlib import Path

from capsule import paths, unblocked

EVENT_KIND = "capsule_unblocked"
COUNT_MISSING_SECONDS = 1500  # no count this long = detector failing (>= tick.CONTRACT_TIMEOUT+300)
CACHE_MAX_AGE_SECONDS = 180  # row 472: 3x bin/capsule-unblocked-hook.sh's own 60s cache TTL
ALERT_NAME = "UnownedWorkIdle"
ALERT_KEY = "unowned_work_idle"
# capsule/tick.py's guard_state key (not the detector's)
LAST_DISPATCH_GUARD = "last_dispatch_at"
# detector doc key -> the emitted metric name (row 470's own names, not the dotted `capsule.*`
# convention maintenance/obs_probe.py's samples use - Grafana reads these as event payload keys).
METRIC_NAMES = {"unblocked": "capsule_unblocked_total", "unowned": "capsule_unowned_total"}
LAST_DISPATCH_METRIC = "capsule_last_dispatch_timestamp"


CACHE_STATES = ("fresh", "stale", "missing", "corrupt")


def cache_state(path=None, *, now=None, max_age=CACHE_MAX_AGE_SECONDS):
    """One of `CACHE_STATES`: the single source of truth
    `read_cache`, `cache_is_stale` and `Tick._check_cache_staleness` all build on, so
    "no usable data this pass" is always exactly one of three DISTINCT, individually-
    nameable reasons instead of one bare boolean that only ever meant "stale": -
    "missing": no file at that path at all (the detector/its hook has never run, or
    its cache dir was wiped) - `max_age` never applies to something that doesn't
    exist."""
    p = Path(path) if path is not None else paths.unblocked_cache()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return "missing"
    if max_age is not None:
        now_ = now if now is not None else time.time()
        if (now_ - mtime) > max_age:
            return "stale"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return "missing"
    try:
        doc = json.loads(text)
    except ValueError:
        return "corrupt"
    return "fresh" if isinstance(doc, dict) else "corrupt"


def cache_is_stale(path=None, *, now=None, max_age=CACHE_MAX_AGE_SECONDS):
    """True only for `cache_state(...) == "stale"` specifically - a
    missing or corrupt cache is "no data" for a different reason, not staleness; see
    `cache_state`'s own docstring for the full state machine."""
    return cache_state(path, now=now, max_age=max_age) == "stale"


def read_cache(path=None, *, now=None, max_age=CACHE_MAX_AGE_SECONDS):
    """The detector's cached doc, or None - a missing file, an unreadable one, invalid JSON, a
    JSON value that is not an object, or a file whose own mtime is stale (`cache_state` is
    anything but "fresh") are all "no data this pass", never an exception."""
    if cache_state(path, now=now, max_age=max_age) != "fresh":
        return None
    p = Path(path) if path is not None else paths.unblocked_cache()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def metrics_payload(doc, last_dispatch_ts=None):
    """{"capsule_unblocked_total": ..., ...} from the detector's doc
    (`unblocked`/`unowned`, kept only when numeric) plus
    `capsule_last_dispatch_timestamp` from capsule's own dispatch memory (kept only
    when numeric) - a partial or malformed doc, or no dispatch memory yet, yields a
    partial (or empty) payload, never a KeyError or a fabricated zero."""
    out = {}
    if isinstance(doc, dict):
        out.update({name: doc[key] for key, name in METRIC_NAMES.items() if _numeric(doc.get(key))})
    if _numeric(last_dispatch_ts):
        out[LAST_DISPATCH_METRIC] = last_dispatch_ts
    return out


_refresh_failing = False    # latched on the first failed refresh, cleared by the next success


def _refresh_cache(path=None, *, now=None, detect=None, detect_args=None):
    """The hook writes the cache only at SessionStart, so it is stale most
    of the time."""
    p = Path(path) if path is not None else paths.unblocked_cache()
    if cache_state(p, now=now) == "fresh":
        return False
    global _refresh_failing
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")    # not the hook's `.tmp`: they may interleave
    try:
        text = unblocked.render_json((detect or unblocked.detect)(**(detect_args or {})))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:
        with contextlib.suppress(OSError):    # NotADirectoryError when the parent is a file
            tmp.unlink()
        if not _refresh_failing:    # one line per outage, not one per 30 s pass
            print(f"capsule tick: unblocked cache refresh failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        _refresh_failing = True
        return False
    _refresh_failing = False
    return True


def export(log, *, path=None, last_dispatch_ts=None, board=None, source="capsule", now=None,
           refresh=False, detect=None, detect_args=None):
    """The tick's step (6c): read the detector's cache, write one `capsule_unblocked`
    event with whatever fields are available this pass (`last_dispatch_ts` is the
    caller's own dispatch memory, never read from the cache - see the module
    docstring)."""
    if refresh:
        _refresh_cache(path, now=now, detect=detect, detect_args=detect_args)
    fields = metrics_payload(read_cache(path, now=now), last_dispatch_ts)
    if not fields:
        return False
    log(EVENT_KIND, source, "capsule: unblocked/unowned snapshot (row 470)", board=board, payload=fields)
    return True
