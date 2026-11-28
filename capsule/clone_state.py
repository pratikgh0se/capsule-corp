"""What a clone carries, uncommitted, when a run is dispatched (inc_1abc7a24)."""
import subprocess

from capsule import workspace


def _entries(out):
    """(code, path) per entry of `git status --porcelain -z`; a rename's old name is skipped."""
    fields, i, found = out.split("\0"), 0, []
    while i < len(fields) and len(fields[i]) > 3:
        code, path = fields[i][:2], fields[i][3:]
        found.append((code, path))
        i += 2 if "R" in code or "C" in code else 1
    return found


def _state(code):
    if code == "??":
        return "untracked"
    return "staged" if code[0] != " " else "unstaged"


def carried(ws):
    """Each uncommitted path of the clone at `ws` as {path, state, protected}. Read only:
    optional locks are off, so the index is not refreshed. None (unknown, never an exception)
    when the dir is not a git clone or git fails: a brief helper must not crash a dispatch."""
    try:
        out = workspace._git("--no-optional-locks", "status", "--porcelain", "-z",
                             *workspace.SCRATCH_EXCLUDE, ws=ws).stdout
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
        return None
    found = _entries(out)
    flagged = set(workspace.touches_protected([p for _, p in found]))
    return [{"path": p, "state": _state(c), "protected": p in flagged} for c, p in found]
