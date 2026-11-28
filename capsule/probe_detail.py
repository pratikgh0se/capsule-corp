"""What a failed container start leaves for the next reader (card t_afb35167, inc_3e98be67).

`CalledProcessError.__str__` names the command and the exit status, never its output, so the tick's
`tick_error` line lost the git message of a failed mount probe. `of` keeps that output on the event,
and `attempt` stops the epic branch of a failed probe for the rest of the dispatch pass."""

import os
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.as_posix()
DETAIL_CHARS = 2000
PROBE_CMD = "mount-probe"       # the cmd `StrongRunner.start` raises its probe failure with


def of(exc):
    """The `tick_error` payload fields for `exc`: `detail` (stderr then output, 2000 chars) for a
    failed mount probe that has any; nothing for any other exception. Only the probe's text is
    bounded by construction (Korin reads the event unscrubbed), so no other command's is kept."""
    if getattr(exc, "cmd", None) != PROBE_CMD:
        return {}
    detail = "\n".join(str(p) for p in (exc.stderr, exc.output) if p)[:DETAIL_CHARS]
    return {"detail": detail} if detail else {}


def where(exc):
    """`<repo-relative path>:<line> <function>` of the innermost frame of `exc`'s traceback that
    lies under the repo (a stdlib or site-packages frame is skipped for its capsule caller); with no
    such frame, the innermost frame's file basename, never an absolute path. No message text and
    no locals, so it is safe on a tick_error whose text is withheld. "" without a traceback."""
    frames = [(f.f_code, n) for f, n in traceback.walk_tb(exc.__traceback__)]
    if not frames:
        return ""
    prefix = f"{ROOT}/"
    inside = [(c, n, os.path.realpath(c.co_filename)) for c, n in frames]
    inside = [(c, n, r[len(prefix):]) for c, n, r in inside if r.startswith(prefix)]
    if not inside:
        inside = [(*frames[-1], os.path.basename(frames[-1][0].co_filename))]
    code, line, name = inside[-1]
    return f"{name}:{line} {code.co_name}"


def fields(exc):
    """The `tick_error` payload fields for `exc`: `where` and whatever `of` keeps."""
    return {"where": where(exc), **of(exc)}


def unskipped(rows, skip):
    """The ready rows whose card id and branch are not in `skip`; checked as each row is reached,
    so a skip added while the pass walks the list holds for the rows after it."""
    return (r for r in rows if r["id"] not in skip and r["branch_name"] not in skip)


def attempt(skip, row, work, *args):
    """Run `work(*args)` for the ready `row`. If it raises, the card joins `skip` for the pass; a
    failed mount probe adds its branch too: every card there shares the clone that failed."""
    try:
        return work(*args)
    except Exception as exc:
        skip.add(row["id"])
        if getattr(exc, "cmd", None) == PROBE_CMD:
            skip.add(row["branch_name"])
        raise
