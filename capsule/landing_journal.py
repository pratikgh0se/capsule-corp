"""The landing's step journal (R12, R05 format) and closing a push it left unrecorded. Split from
landing.py for its size; landing is the only caller."""
import sys

from capsule import journal, paths
from capsule.landing_reverts import _unrecorded_push


def write(cfg, task_id, home, step, **fields):
    """R12: the landing's step journal (R05 format) at `<home>/landing/<board>/<task>/`. `push`
    is written BEFORE the push, `landed` once the landing is recorded, so a kill in between
    leaves a push with no `landed` after it. Never fatal: without it landing works as before."""
    spool = paths.home(home) / "landing" / cfg["board"] / task_id
    try:
        spool.mkdir(parents=True, exist_ok=True)
        journal.append(spool, step, **fields)
    except Exception as exc:
        print(f"landing: journal skipped {step}: {exc}", file=sys.stderr)


def abandon(cfg, task_id, home, release):
    """Close the journal's pending push of a card a human reverted on main: it is left alone."""
    if pending := _unrecorded_push(cfg, task_id, home, release):
        write(cfg, task_id, home, "push_abandoned", sha=pending["sha"])


def unrecorded(cfg, task_id, home, release):
    """The journal's push of this card that reached origin/main with no landing recorded
    (`{"sha": ...}`), or None; only a lander in `land` mode ever has one."""
    if (cfg.get("lander") or {}).get("mode") != "land":
        return None
    return _unrecorded_push(cfg, task_id, home, release)
