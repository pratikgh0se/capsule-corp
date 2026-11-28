"""Single-writer tick guard (row 517)."""
import errno
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from capsule import config, paths

WAIT_TIMEOUT_S = 10.0
WAIT_INTERVAL_S = 0.2


def _default_board(home):
    cfg, _ = config.Loader(paths.config_path(home)).get()
    return (cfg or {}).get("board") or "default"


def lock_path(home, board=None):
    return Path(home) / f"tick-{board or _default_board(home)}.lock"


def board_guard(home, board):
    """`(True, None)` when `boards/<board>/config.json` exists, else `(False, "no config for
    board <board>; nothing to serve")` - ruling Q9: never take the lock for an unconfigured board."""
    if paths.config_path(home, board).is_file():
        return True, None
    return False, f"no config for board {board}; nothing to serve"


def held(home, board=None):
    """Read-only probe: True while `lock_path(home, board)` is exclusively held by anyone (a fresh
    fd's own `LOCK_SH|LOCK_NB`, released right after)."""
    path = lock_path(home, board)
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# -- the lock: sole authority for guard_start ----------------------------------------------

def _open_lock_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.fdopen(os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644), "r+")


def _try_acquire(path):
    """The open, locked file handle, or None (someone else holds it)."""
    f = _open_lock_file(path)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        f.close()
        if exc.errno not in (errno.EAGAIN, errno.EACCES):    # not contention: a real failure
            raise
        return None
    return f


def _holder_pid(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _lock_state(path):
    """(held, holder_pid) - held only when we could not take the lock ourselves; a crashed
    holder's stale pid text is never trusted once the lock is ours for the taking."""
    f = _try_acquire(path)
    if f is None:
        return True, _holder_pid(path)
    fcntl.flock(f, fcntl.LOCK_UN)
    f.close()
    return False, None


def _wait_for_lock(path, sleep, monotonic, timeout_s=WAIT_TIMEOUT_S, interval=WAIT_INTERVAL_S):
    deadline = monotonic() + timeout_s
    while True:
        f = _try_acquire(path)
        if f is not None:
            return f
        if monotonic() >= deadline:
            return None
        sleep(interval)


def guard_start(home, board=None, *, replace=False, kill=None, sleep=time.sleep, monotonic=time.monotonic):
    """Refuses (`SystemExit(3)`) when `$CAPSULE_HOME/tick-<board>.lock` is already held,
    unless `replace`: SIGTERM the holder, wait up to 10s for the lock to free, and
    refuse anyway if it does not (never start beside a live tick, never SIGKILL)."""
    kill = kill or os.kill
    path = lock_path(home, board)
    f = _try_acquire(path)
    if f is None:
        holder = _holder_pid(path)
        if not replace:
            print(f"tick: refusing, pid {holder} already running" if holder is not None else
                  "tick: refusing, another tick already holds the lock", file=sys.stderr)
            raise SystemExit(3)
        if holder is not None:
            try:
                kill(holder, signal.SIGTERM)
            except ProcessLookupError:
                pass    # already gone
            except PermissionError:
                print(f"tick: refusing, cannot signal pid {holder} (permission denied)", file=sys.stderr)
                raise SystemExit(3)
        f = _wait_for_lock(path, sleep, monotonic)
        if f is None:
            print(f"tick: refusing, pid {holder} did not exit within {WAIT_TIMEOUT_S:g}s", file=sys.stderr)
            raise SystemExit(3)
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()}\n")
    f.flush()
    return f


# -- argv: the second oracle for `capsule check` only (Z2), never authoritative -------------

def _ps(run_cmd):
    """[(pid, ppid, argv)], or None when `ps` could not be observed."""
    try:
        out = run_cmd(["ps", "-Awwo", "pid,ppid,args"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    rows = []
    for line in out.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return rows


def _is_tick_argv(argv):
    """True for a real `-m capsule tick` invocation or the `capsule tick` console-script tokens -
    never a substring inside one argument (a shell wrapper or a prompt mentioning the words
    must not match)."""
    tokens = argv.split()
    if any(tokens[i:i + 3] == ["-m", "capsule", "tick"] for i in range(len(tokens) - 2)):
        return True
    return len(tokens) >= 2 and Path(tokens[0]).name == "capsule" and tokens[1] == "tick"


def _ancestors(rows, pid):
    ppid_of = {p: pp for p, pp, _ in rows}
    out = {pid}
    for _ in range(64):     # generous ceiling; a real process tree is never this deep
        pid = ppid_of.get(pid)
        if pid is None or pid in out:
            break
        out.add(pid)
    return out


def live_ticks(run_cmd=subprocess.run, *, exclude_pid=None):
    """[(pid, argv)] for every live process `_is_tick_argv` matches, excluding `exclude_pid` and
    its whole ancestor chain. None when `ps` could not be observed - unknown, never zero."""
    rows = _ps(run_cmd)
    if rows is None:
        return None
    excluded = _ancestors(rows, exclude_pid) if exclude_pid is not None else set()
    return [(pid, argv) for pid, _, argv in rows if pid not in excluded and _is_tick_argv(argv)]


def _flag(tokens, name):
    """The value of `name VALUE` or `name=VALUE` in an argv token list, else None."""
    for i, t in enumerate(tokens):
        if t == name and i + 1 < len(tokens):
            return tokens[i + 1]
        if t.startswith(name + "="):
            return t[len(name) + 1:]
    return None


def _serving(ticks, home, board):
    """Pids of `ticks` serving `board` in `home`: no --board means the home's default board,
    no --capsule-home means paths.home() (one predicate for dispatcher and check_row)."""
    home = Path(home).resolve()
    return [pid for pid, argv in ticks
            if Path(_flag(argv.split(), "--capsule-home") or paths.home()).resolve() == home
            and (_flag(argv.split(), "--board") or _default_board(home)) == board]


def dispatcher(home, board, *, run_cmd=subprocess.run):
    """`live` when the lock and the argv scan agree on one tick serving `board` in `home`, `none`
    when neither sees one, `uncertain` otherwise (Z2: the lock alone never decides)."""
    home = Path(home)
    ticks = live_ticks(run_cmd)
    if ticks is None:
        return "uncertain"
    pids = _serving(ticks, home, board)
    locked = held(home, board)
    if locked and pids == [_holder_pid(lock_path(home, board))]:
        return "live"
    return "none" if not locked and not pids else "uncertain"


def check_row(home, *, board=None, run_cmd=subprocess.run, self_pid=None):
    """(level, "tick", detail) for `capsule check`, per board: the lock (authoritative) and the
    argv scan (second oracle) must agree on exactly one pid, or the row FAILs."""
    board = board or _default_board(home)
    held, lock_pid = _lock_state(lock_path(home, board))
    argv_pids = live_ticks(run_cmd, exclude_pid=self_pid if self_pid is not None else os.getpid())
    if argv_pids is None:
        return "FAIL", "tick", "ps unavailable"
    pids = _serving(argv_pids, home, board)
    if held and pids == [lock_pid]:
        return "ok", "tick", f"lock held by pid {lock_pid}"
    if not held and not pids:
        return "FAIL", "tick", "no tick (lock free)"
    where = f"lock pid {lock_pid}" if held else "lock free"
    return "FAIL", "tick", f"{where} vs argv pids {pids} disagree"
