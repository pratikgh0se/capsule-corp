"""The process and file facts the stall remedies use, each injectable (`stall_remedies`): the
worker is alive (`alive(pid)`), the run is quiet (`quiet_s(run_id)`: seconds since a spool file or
the workspace changed, None when unknown), the CPU seconds of a pid's process tree (`cpu_s(pid)`,
None when unreadable), the Docker state (`docker_up`, a bool or a callable), `kill(pid)`,
`sleep`."""
import contextlib
import json
import os
import subprocess
from pathlib import Path

from capsule import docker_down, paths

DAY, LOOK_S, SCAN_CAP = 86400, 300, 4000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv"}


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _secs(text):
    """`ps` CPU time: `M:SS.cc`, `H:MM:SS` or `D-HH:MM:SS`."""
    days, _, rest = text.rpartition("-")
    total = 0.0
    for part in rest.split(":"):
        total = total * 60 + float(part)
    return total + (int(days) * DAY if days else 0)


def _ps():
    try:
        return subprocess.run(["ps", "-axo", "pid=,ppid=,time="], capture_output=True, text=True,
                              timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _tree_map(pid, table=None):
    """CPU seconds of `pid` and each descendant as `{pid: seconds}`, None when unreadable. `table`:
    the `ps` text of the pass (one `ps` call per pass, not per run)."""
    kids, cpu = {}, {}
    for row in (line.split() for line in (_ps() if table is None else table).splitlines()):
        if len(row) == 3 and row[0].isdigit() and row[1].isdigit():
            kids.setdefault(int(row[1]), []).append(int(row[0]))
            with contextlib.suppress(ValueError):
                cpu[int(row[0])] = _secs(row[2])
    todo, tree = [pid], {}
    while todo:
        cur = todo.pop()
        if cur in cpu:
            tree[cur] = cpu[cur]
        todo += kids.get(cur, [])
    return tree if pid in cpu else None


def _tree_cpu(pid, table=None):
    """CPU seconds used by `pid` and its descendants, None when unreadable."""
    tree = _tree_map(pid, table)
    return None if tree is None else sum(tree.values())


def _newest(root):
    """Newest mtime under `root` (a bounded walk that skips `.git` and the like), 0 when none."""
    newest, seen = 0.0, 0
    for here, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            try:
                newest = max(newest, os.lstat(os.path.join(here, name)).st_mtime)
            except OSError:
                continue
            seen += 1
        if seen > SCAN_CAP:
            break
    return newest


def _default_quiet(cfg, now, probes):
    root = Path(probes.get("spool_root") or paths.board_dir(cfg.get("board") or "") / "spool")

    def quiet(run_id):
        """Seconds since the spool (journal, `worker.log`, spool files) or, once that is quiet
        for LOOK_S, the run's workspace changed. None: no spool, or the run already finished."""
        sp = root / run_id
        if not sp.is_dir() or (sp / "run.json").exists():
            return None
        mark = max(_newest(sp), os.lstat(sp).st_mtime)
        if now - mark >= LOOK_S:
            try:
                work = json.loads((sp / "job.json").read_text()).get("workspace")
            except (OSError, ValueError):
                work = None
            mark = max(mark, _newest(work) if work and os.path.isdir(work) else 0)
        return max(0, now - mark)
    return quiet


def resolve(conn, cfg, now, probes, tick=None):
    """The probe table: what the caller injected, else the real thing."""
    p = dict(probes or {})
    p.setdefault("alive", getattr(tick, "alive", None) or _alive)
    memo = []

    def table():
        if not memo:
            memo.append(_ps())
        return memo[0]
    if "cpu_s" not in p:        # the real probe also gives the per-pid map (`stall_silence`)
        p["cpu_s"] = lambda pid: _tree_cpu(pid, table())
        p["cpu_map"] = lambda pid: _tree_map(pid, table())
    p.setdefault("quiet_s", _default_quiet(cfg, now, p))
    p.setdefault("docker_up", lambda: docker_down.state(conn, now) is None)
    return p
