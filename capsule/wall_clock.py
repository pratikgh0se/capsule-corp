"""The wall-clock cap starts at the real spawn, not the pass start (inc_b67d8566)."""
from capsule import journal


def launch_time(now, clock):
    """The launched_at to write: a fresh clock read at spawn, never before the pass start."""
    return max(now, int(clock()))


def counted_from(spool, launched):
    """The later of launched (launched_at, else started_at) and the journal "start" step."""
    starts = [int(s["at"]) for s in journal.read(spool) if s.get("step") == "start"]
    return max([int(launched), *starts])
