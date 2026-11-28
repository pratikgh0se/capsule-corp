"""The tick's boundary step: restart a tick onto landed code (off-the-old-system C07c)."""
import ast
import json
import os
import sqlite3
import subprocess
import sys

from capsule import docker_door, incidents, outbox, paths
from capsule.db import guard_get, guard_set
from capsule.lander import _git

KEY = "tick_restart_pending"
QUIET = "1500"      # --quiet seconds: over tick.CONTRACT_TIMEOUT (1200) plus slack
GAP_WAIT = 45 * 60
FIRE_TTL = 45 * 60    # a restart: QUIET + capsule-tick.sh WAIT (900) + startup
MAINT = ("maintenance/events.py", "maintenance/vault_git.py")
SCRIPT = paths.REPO / "bin" / "capsule-tick.sh"


def _imports(source):
    """The `capsule.<module>` names one source file imports."""
    found = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return found                    # a file that does not parse imports nothing we can follow
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "capsule":
                found.update(a.name for a in node.names)
            elif node.module.startswith("capsule."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            found.update(a.name.split(".")[1] for a in node.names if a.name.startswith("capsule."))
    return found


def mark(conn, repo, base, sha, cfg, now):
    """Flag a restart when `base..sha` touches code the tick loads. True when the flag is set."""
    rc, out = _git(repo, "diff", "--name-only", f"{base}..{sha}")
    loaded, todo = set(), ["tick", "__main__"]    # what `-m capsule tick` loads, transitively
    while todo:
        name = todo.pop()
        rc_show, source = _git(repo, "show", f"{sha}:capsule/{name}.py")
        if not rc_show and name not in loaded:
            loaded.add(name)
            todo.extend(_imports(source))
    files = {f"capsule/{n}.py" for n in loaded} | {"capsule/__init__.py", *MAINT}
    if rc or not set(out.split()) & files:
        return False
    old, _ = guard_get(conn, KEY)
    boards = [cfg["board"]] + list((cfg.get("lander") or {}).get("restart_with", []))
    since = (old or {}).get("since", now)
    guard_set(conn, KEY, {"sha": sha, "since": since, "boards": boards}, now=now)
    return True


def _contains(head, sha):
    """True when `head` (a sha in this process's checkout) has `sha` in its history."""
    return bool(head) and subprocess.run(
        ["git", "-C", str(paths.REPO), "merge-base", "--is-ancestor", sha, head],
        capture_output=True).returncode == 0


# what THIS process imported; the disk moves before the kickstart
BOOT = incidents.main_head(paths.REPO)
_CHECKED = [BOOT]                  # the last disk HEAD the self-mark looked at


def _has(sha):
    """True when this PROCESS booted on a checkout containing `sha`: BOOT, never a live rev-parse
    (`restart` fast-forwards the checkout before it kickstarts, and the old tick must not count)."""
    return _contains(BOOT, sha)


def open_runs(board, home):
    """Open `task_runs` on `board` (an unreadable board counts as busy)."""
    try:
        db = sqlite3.connect(f"file:{paths.board_db(board, home)}?mode=ro", uri=True)
        return db.execute("SELECT COUNT(*) FROM task_runs WHERE ended_at IS NULL").fetchone()[0]
    except sqlite3.Error:
        return 1


def _gap(board, home):
    """A run-one gap on `board`: no open run, no worker container (one with a `capsule-run-id`
    label; gate and test containers also carry `capsule=1` but never hold a gap), no run-one."""
    if open_runs(board, home):
        return False
    try:
        box = docker_door.docker_run(["ps", "-q", "--filter", "label=capsule-run-id"],
                                     capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        box = ""                        # docker down: nothing is running in it
    pgrep = subprocess.run(["pgrep", "-f", "capsule run-one"], capture_output=True)
    return not box and pgrep.returncode == 1


def _env(home):
    return {**os.environ, "CAPSULE_HOME": str(home)}


def _status(home, board):
    """`capsule-tick.sh status <board>` as a dict ({} when it cannot be read)."""
    try:
        done = subprocess.run([str(SCRIPT), "status", board], capture_output=True, text=True,
                              env=_env(home), timeout=60)
        return json.loads(done.stdout)
    except (ValueError, OSError, subprocess.SubprocessError):
        return {}


def _fire(home, board):
    """`restart <board> --quiet 1500`, detached (it outlives a tick that restarts itself)."""
    log = home / "logs" / f"boundary-{board}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        subprocess.Popen([str(SCRIPT), "restart", board, "--quiet", QUIET], stdout=out,
                         stderr=out, env=_env(home), start_new_session=True)


def step(conn, cfg, home, now, **effects):
    """One boundary pass (`none | waiting | fired | done | failed | error`); never raises: a
    broken boundary step is a stderr line, not a lost tick pass."""
    try:
        return _step(conn, cfg, home, now, **effects)
    except Exception as exc:
        print(f"capsule tick: boundary step failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return "error"


def _step(conn, cfg, home, now, *, gap=_gap, fire=_fire, status=_status, loaded=_has):
    flag, _ = guard_get(conn, KEY)
    if not flag or not flag["boards"]:
        head = incidents.main_head(paths.REPO)    # disk moved, not by a landing
        if head and BOOT and head != _CHECKED[0] and _contains(head, BOOT):
            _CHECKED[0] = head
            mark(conn, paths.REPO, BOOT, head, {"board": cfg["board"]}, now)
            flag, _ = guard_get(conn, KEY)
    if not flag or not flag["boards"]:
        return "none"
    if flag.get("failed"):
        return "failed"
    flag, own, sha = dict(flag), cfg["board"], flag["sha"]
    boards, fired = list(flag["boards"]), flag.get("fired")
    here = loaded(sha)
    if boards[0] == own and here:
        boards.pop(0)
        fired = None
    elif fired and fired["board"] != own and fired["board"] == boards[0]:
        up = status(home, fired["board"])
        if up.get("alive") and up.get("pid") != fired["pid"] and _contains(up.get("main_sha"), sha):
            boards.pop(0)
            fired = None
    if not boards:
        guard_set(conn, KEY, None, now=now)
        return "done"
    if fired and now - fired["at"] > FIRE_TTL:
        text = f"tick restart of {fired['board']} did not come back on {sha[:12]}"
        outbox.notify(conn, "needs_input", text, fields=[("log", f"boundary-{fired['board']}.log")],
                      file_body=f"capsule: {text}", now=now)
        guard_set(conn, KEY, {**flag, "boards": boards, "failed": True}, now=now)
        return "failed"
    if not fired and (boards[0] == own or here) and gap(boards[0], home):
        fired = {"board": boards[0], "at": now, "pid": status(home, boards[0]).get("pid")}
        # recorded before the fire: a tick that restarts itself never gets to write it after
        guard_set(conn, KEY, {**flag, "boards": boards, "fired": fired}, now=now)
        fire(home, boards[0])
        return "fired"
    if not fired and now - flag["since"] > GAP_WAIT and not flag.get("warned"):
        text = f"restart pending ({boards[0]}) on {sha[:12]}: no run-one gap in 45 min"
        outbox.notify(conn, "scheduled", text, file_body=f"capsule: {text}", now=now)
        flag["warned"] = True
    guard_set(conn, KEY, {**flag, "boards": boards, "fired": fired}, now=now)
    return "waiting"
