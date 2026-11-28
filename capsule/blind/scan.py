"""The names a card's code gives away, for the blind tester's leak check (R30). Private helpers of
`capsule.blind.dispatch`, split out for its size: `_identifiers` reads a diff, `_work_names` reads
the shared epic clone read-only."""
import os
import re
import stat
from pathlib import PurePosixPath

from capsule import workspace

_DEF = re.compile(r"^\+\s*(?:async\s+)?(?:def|class)\s+(\w+)")
_CONST = re.compile(r"^\+\s*([A-Z][A-Z0-9_]+)\s*(?::[^=]+)?=")
MAX_READ = 200_000        # bytes read of one untracked file


def _identifiers(diff_text):
    """Names a diff gives away: changed file paths (full path, basename, stem, dotted module) and
    the `def`, `class` and UPPER_CASE constant names on added lines."""
    found = set()
    for line in diff_text.splitlines():
        path = line[4:].split("\t")[0]
        if line.startswith("diff --git "):
            found.update(re.findall(r" [ab]/(\S+)", line))
        elif line.startswith(("--- ", "+++ ")) and path != "/dev/null":
            found.add(path[2:] if path[:2] in ("a/", "b/") else path)
        elif m := (_DEF.match(line) or _CONST.match(line)):
            found.add(m.group(1))
    for path in [f for f in found if "/" in f or "." in f]:
        p = PurePosixPath(path)
        found.update({p.name, p.stem})
        if p.suffix == ".py":
            found.add(".".join(p.with_suffix("").parts))
    return found


def _head(path):
    """The first `MAX_READ` bytes of a regular file as text; "" for a link, a FIFO, a directory or
    anything unreadable (the container owns these files: never followed, never blocked on)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            regular = stat.S_ISREG(os.fstat(f.fileno()).st_mode)
            return f.read(MAX_READ).decode(errors="replace") if regular else ""
    except OSError:
        return ""


def _work_names(epic, pinned):
    """The names the card's own work gives away: the shared epic clone `epic`'s diff from `pinned`
    plus its untracked files (a fresh clone never holds that work). Read-only:
    `--no-optional-locks` and `diff.autoRefreshIndex=false` keep git off the index a running
    implementer's `git ls-files` reads; `_clone` and `workspace.diff` are never used. No pinned
    start or no clone yet: none."""
    if not pinned or not workspace.gitdir(epic).is_dir():
        return set()

    def git(*args):
        return workspace._git("--no-optional-locks", "-c", "diff.autoRefreshIndex=false", *args,
                              ws=epic).stdout
    new = git("ls-files", "--others", "--exclude-standard", "-z", *workspace.SCRATCH_EXCLUDE)
    diff = git("diff", "--no-ext-diff", "--no-textconv", "--end-of-options", pinned)
    for path in filter(None, new.split("\0")):
        lines = _head(epic / path).splitlines()
        diff += f"diff --git a/{path} b/{path}\n" + "".join(f"+{ln}\n" for ln in lines)
    return _identifiers(diff)
