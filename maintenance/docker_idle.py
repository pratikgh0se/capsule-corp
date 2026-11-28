#!/usr/bin/env python3
"""Docker idle reaper (decisions D195, D204): the selected Docker engine (Docker Desktop or
OrbStack, see docker_engine.py) runs only while something needs it, and is quit once it has been
idle for a while. launchd runs it with --apply every 5 minutes. No flag or --dry-run prints
what it would do and changes nothing (not even the idle clock).

Every run: the engine's daemon not answering `docker info` -> exit 0 quietly. Otherwise "busy"
if ANY of (CHECKS, first reason wins):
- a running container that is not part of the always-on stack (label
  com.docker.compose.project=capsule-corp); this covers worker (capsule.kind=run,
  capsule-run-id), test, blind and lander containers, and anything that is not Capsule's at all;
- any container event (the stack's own excluded) in the last 10 min
  (`docker events --since/--until`, RECENT_S): a 5-minute poll never sees a short `docker run
  --rm`, so a keeper gate or contract job running them back to back would look idle between polls;
- a landing in progress, failing closed like capsule/landing.py _keeper_busy at ANY age:
  $CAPSULE_HOME/queue/<repo>/owners.json holds a merge-keeper claim, or a queue marker file says
  state "landing". One older than 4 h still holds Docker up and prints "stale claim pins Docker";
- per board (boards/*/kanban.db, read only): the board is not paused and capsule/docker_down.py
  waiting() is not empty (the inverse of the Z5 detector: cards wait on a container), or a slack
  outbox row is pending and not given up (Docker stays up for ANY such row; the token is read
  from the Keychain first (D227), Infisical, which stops with the stack, is only the fallback);
- a `docker build`, `buildx`, `image build`, `builder build`, `compose build` or
  `compose up --build` process;
- a test suite process: a command word whose basename is `capsule-suite` or `run_shell_suites.sh`
  (a keeper gate starts short containers with gaps between them, inc_5cde81c1).
  If `ps` fails, that also counts as busy ("process list unavailable"): the reaper cannot tell.
Busy clears the idle clock. A gap of over 10 min since the last run (sleep, a missed run) also
restarts it. Idle continuously for OPS_DOCKER_IDLE_MIN minutes (default 15), with --apply:
`docker compose -p capsule-corp stop` (graceful; containers, volumes and data stay), CHECKS again
(a container may have started meanwhile: then Docker stays up and the clock resets), then
`osascript -e 'quit app "<engine app>"'` (Docker or OrbStack) and a quit stamp
(<state root>/docker-idle-quit) that heal.sh
reads so Docker being down is a skip, not a page. If the stop fails Docker is not quit. Never rm,
rmi, prune, down -v or kill. Lesson: Docker Desktop must not be reopened until its "Docker Desktop"
processes are gone (an early open leaves an error dialog). Only `StrongRunner._ensure_engine`
(capsule/runners/docker.py) starts the engine again, before a container start. A running run
(under 2 h old) on a board that is not paused also counts as busy.
State: <events.state_root()>/docker_idle.state (idle_since, last_seen); actions: docker_idle.jsonl.
Exit 0 always except 1 when the compose stop or the quit failed.
"""
import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events   # noqa: E402  docker_run() and state_root(), the doors every maintenance job uses
from capsule import db, dispatch, docker_down, outbox, paths   # noqa: E402

PROJECT = "capsule-corp"
COMPOSE = "com.docker.compose.project"
KEEPER = "merge-keeper"             # capsule/lander.py KEEPER
LOUD_S = 4 * 3600                   # a claim or marker this old is still honoured, and named
RECENT_S = 600                      # container events this recent keep the engine up
OPEN_RUN_S = 2 * 3600                # a run row open longer than this is stale, not busy
GAP_S = 600                         # two 5-minute runs: a longer gap is not "continuously idle"
TIMEOUT = 120
SLACK_MAX = outbox.MAX_ATTEMPTS["slack"]
FLAGS_WITH_VALUE = {"--context", "-c", "-H", "--host", "--config", "-l", "--log-level",
                    "--tlscacert", "--tlscert", "--tlskey"}
BUILDERS = {"docker", "docker-compose", "docker-buildx"}


def _now():
    return time.time()


def _run(cmd):
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=TIMEOUT)


def docker(*args):
    return events.docker_run(list(args), capture_output=True, text=True, timeout=TIMEOUT)


def _state():
    return events.state_root() / "docker_idle.state"


def log(what, result):
    print(f"docker-idle: {what} -> {result}")
    path = events.state_root() / "docker_idle.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "what": what,
                                "result": result}) + "\n")
    except OSError as e:
        print(f"docker-idle: log write failed: {e.__class__.__name__}")


def set_idle(since, now=None):
    p = _state()
    if since is None:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"idle_since": since, "last_seen": now}))
    os.replace(tmp, p)


def _subcommands(rest):
    """The words of `rest` that are not flags (nor the value of a flag that takes one)."""
    subs, skip = [], False
    for w in rest:
        if skip:
            skip = False
        elif w in FLAGS_WITH_VALUE:
            skip = True
        elif not w.startswith("-"):
            subs.append(w)
    return subs


def _build_cmd(exe, subs, rest):
    if not subs:
        return False
    if subs[0] in ("build", "buildx") or (exe == "docker-buildx" and {"build", "bake"} & set(subs)):
        return True
    if subs[0] in ("image", "builder") and subs[1:2] == ["build"]:
        return True
    if subs[0] == "compose" or exe == "docker-compose":
        return "build" in subs or ("up" in subs and "--build" in rest)
    return False


def _is_build(line):
    """True for a docker build style command line (global flags before the subcommand are
    skipped; so is the plugin metadata probe Docker runs on every command)."""
    words = line.split()
    if "docker-cli-plugin-metadata" in words:
        return False
    for i, word in enumerate(words):
        exe = os.path.basename(word)
        if exe in BUILDERS and _build_cmd(exe, _subcommands(words[i + 1:]), words[i + 1:]):
            return True
    return False


def _containers_busy(now):
    ps = docker("ps", "--format", '{{.Names}}\t{{.Label "' + COMPOSE + '"}}')
    if ps.returncode != 0:
        return "docker ps failed"          # cannot tell: stay up
    for line in ps.stdout.splitlines():
        name, _, project = line.partition("\t")
        if name and project.strip() != PROJECT:
            return f"container {name} is running (not the {PROJECT} stack)"
    return ""


def _recent_busy(now):
    """Any container event (the compose stack's own aside) within RECENT_S; sampling `ps` misses
    containers that start and exit between two polls."""
    try:
        ev = docker("events", "--since", str(int(now - RECENT_S)), "--until", str(int(now)),
                    "--filter", "type=container",
                    "--format", '{{index .Actor.Attributes "' + COMPOSE + '"}}')
    except (OSError, subprocess.TimeoutExpired):
        return "docker events failed"
    if ev.returncode != 0:
        return "docker events failed"      # cannot tell: stay up
    if any(line.strip() != PROJECT for line in ev.stdout.splitlines()):
        return f"a container ran in the last {RECENT_S // 60} min"
    return ""


def _landing_busy(now):
    queue = paths.home() / "queue"
    for why, age, _ in paths.keeper_claims(sorted(queue.glob("*/owners.json")), queue, now):
        if age > LOUD_S:
            print(f"docker-idle: stale claim pins Docker ({age / 3600:.1f} h)")
        return why
    return ""


def _board_reason(kanban, now):
    with contextlib.closing(db.connect_ro_tolerant(kanban)) as conn:
        if not dispatch.board_paused(conn, now):     # a pause frees the engine
            if docker_down.waiting(conn):            # includes every card in review
                return "cards are waiting on a container"
            if conn.execute("SELECT 1 FROM task_runs WHERE status='running' AND started_at>?"
                            " LIMIT 1", (now - OPEN_RUN_S,)).fetchone():
                return "a run is open"
        if conn.execute("SELECT 1 FROM outbox WHERE status='pending' AND channel='slack' "
                        "AND attempts < ? LIMIT 1", (SLACK_MAX,)).fetchone():
            return "a Slack message is waiting to be sent"
    return ""


def _boards_busy(now):
    """Cards waiting on a container, or Slack rows not yet sent, on any board; an unreadable
    board counts as busy (cannot tell)."""
    for kanban in sorted((paths.home() / "boards").glob("*/kanban.db")):
        try:
            why = _board_reason(kanban, now)
        except Exception as e:          # noqa: BLE001 - any read problem: stay up
            why = f"unreadable ({e.__class__.__name__})"
        if why:
            return f"board {kanban.parent.name}: {why}"
    return ""


def _procs():
    """(lines, why): the process list, or a busy reason when `ps` fails (cannot tell: stay up)."""
    try:
        return _run(["ps", "-axo", "command="]).stdout.splitlines(), ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], f"process list unavailable: {e.__class__.__name__}"


def _builds_busy(now):
    procs, why = _procs()
    for line in procs:
        if "docker_idle" not in line and _is_build(line):
            return f"build running: {line.strip()[:80]}"
    return why


SUITES = ("capsule-suite", "run_shell_suites.sh")


def _suites_busy(now):
    procs, why = _procs()
    for line in procs:
        if "docker_idle" not in line and any(
                os.path.basename(w) in SUITES for w in line.split()):
            return f"docker test suite running: {line.strip()[:80]}"
    return why


# first reason wins
CHECKS = (_containers_busy, _recent_busy, _landing_busy, _boards_busy, _builds_busy,
          _suites_busy)


def _busy(now):
    return next(filter(None, (c(now) for c in CHECKS)), "")


def _daemon_down():
    try:
        return docker("info").returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return True


def _idle_minutes(now, apply):
    """Minutes idle so far; starts (or, after a run gap, restarts) the clock, --apply only."""
    try:
        doc = json.loads(_state().read_text())
        since, seen = float(doc["idle_since"]), float(doc["last_seen"])
    except (OSError, ValueError, KeyError, TypeError):
        since = seen = None
    if since is None or since > now or now - seen > GAP_S:
        since = now
    if apply:
        set_idle(since, now)
    return (now - since) / 60


def _reap():
    """Stop the compose stack, then quit the engine app; 0 done or backed off, 1 a failure."""
    app = events.docker_engine_row()["app"]
    stop = docker("compose", "-p", PROJECT, "stop")
    if stop.returncode != 0:
        log(f"compose -p {PROJECT} stop", f"FAILED rc={stop.returncode}, Docker left running")
        return 1
    log(f"compose -p {PROJECT} stop", "ran")
    why = _busy(_now())
    if why:     # something started meanwhile: Docker stays up; heal restarts the stack
        log(f'quit app "{app}"', f"skipped, busy: {why}")
        set_idle(None)
        return 0
    exe = shutil.which("osascript")
    if "unittest" in sys.modules and not (       # a test must never reach the real one
            exe and os.environ.get("AGENT_OPS_DOCKER_BIN")
            and os.path.dirname(exe) == os.environ["AGENT_OPS_DOCKER_BIN"]):
        raise RuntimeError("a test resolved the real osascript: use a fake in AGENT_OPS_DOCKER_BIN")
    quit_ = _run([exe or "osascript", "-e", f'quit app "{app}"'])
    log(f'quit app "{app}"', "ran" if quit_.returncode == 0 else f"FAILED rc={quit_.returncode}")
    if quit_.returncode == 0:
        (events.state_root() / "docker-idle-quit").write_text(str(int(_now())))
    set_idle(None)
    return 0 if quit_.returncode == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    apply = ap.parse_args(argv).apply
    need = float(os.environ.get("OPS_DOCKER_IDLE_MIN") or 15)
    now = _now()
    down = _daemon_down()
    why = "" if down else _busy(now)
    if why:
        print(f"docker-idle: busy, {why}")
    if down or why:
        if apply:
            set_idle(None)
        return 0
    idle = _idle_minutes(now, apply)
    if idle < need:
        print(f"docker-idle: idle {idle:.0f} of {need:.0f} min, waiting")
        return 0
    if not apply:
        title = events.docker_engine_row()["title"]
        print(f"docker-idle: would run docker compose -p {PROJECT} stop, then quit {title}")
        return 0
    return _reap()


if __name__ == "__main__":
    sys.exit(main())
