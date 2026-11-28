"""`capsule board tick <name> [--dry-run] [--yes]`: get a board's tick job ready without deploy.sh.

For a board made by `capsule board new` it (1) makes sure $CAPSULE_HOME, its logs folder and the
worker dependency folder exist, (2) clones this checkout's `origin` into
$CAPSULE_HOME/harness/main, the folder the tick runs from (a checkout that is already there is
never pulled or reset; one that is there with no commit is refused, and a clone that fails is
removed again), and (3) writes the tick plist into ~/Library/LaunchAgents from launchd/board/ (a
differing or symlinked file is left alone), once the harness is ready. The plist runs the
interpreter that runs `board tick` (the one the `capsule` launcher pins), never a bare `python3`: a
stock Mac's is 3.9. It never runs launchctl: the last line printed is the command that starts it.

Same output vocabulary as `capsule install` (ok, done, WOULD, skip, next, FAILED), the same
prompts and the same rules; a second run prints only ok, skip and next lines.

Label prefix: CAPSULE_LAUNCHD_PREFIX, else host.json, else dev.capsule. bin/capsule-tick.sh
resolves it the same way (bin/lib_prefix.sh reads host.json), so a run still refuses when other
Capsule plists use a different prefix, and the start line it prints carries
CAPSULE_LAUNCHD_PREFIX whenever the prefix is not dev.capsule.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys

from capsule import cli_ops, install, paths, plists
from capsule.runners import docker

TEMPLATE = paths.REPO / "launchd" / "board" / "__PREFIX__.capsule-tick-__BOARD__.plist"
GIT_SECONDS = 600
PREFIX_OK = re.compile(r"[A-Za-z0-9.-]+")        # what bin/lib_prefix.sh accepts
JOB_FILE = re.compile(r"(.+?)\.capsule-.+\.plist")   # <prefix>.capsule-<job>.plist


def _origin():
    """This checkout's origin url, or the checkout's own path when it has no origin."""
    try:
        done = subprocess.run(["git", "-C", str(paths.REPO), "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return str(paths.REPO)
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else str(paths.REPO)


def _ready(harness):
    """True when `harness` is a git checkout with a commit (a killed clone has none)."""
    if not (harness / ".git").exists():
        return False
    try:
        done = subprocess.run(["git", "-C", str(harness), "rev-parse", "--verify", "-q", "HEAD"],
                              capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _clone(url, harness):
    """None when `url` was cloned into `harness` and it is ready, else why not. `harness` did not
    exist before this run, so a folder a failed or killed clone left there is ours: removed."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:      # cwd: a relative origin (`../x`) is relative to this checkout, as `git remote` says
        done = subprocess.run(["git", "clone", "-q", "--", url, str(harness)], env=env,
                              cwd=paths.REPO, capture_output=True, text=True,
                              timeout=GIT_SECONDS)
        why = None if done.returncode == 0 else (done.stderr.strip().splitlines()
                                                 or ["no output"])[-1]
    except (OSError, subprocess.SubprocessError) as exc:
        why = str(exc)
    if why is None and not _ready(harness):
        why = "the origin has no commit to check out"
    if why is not None and harness.is_dir() and not harness.is_symlink():
        shutil.rmtree(harness, ignore_errors=True)
    return why


def _step_harness(plan):
    """True when the harness is a ready checkout afterwards (never true on a dry run's WOULD)."""
    harness = plan.home / "harness" / "main"
    if os.path.lexists(harness):
        if _ready(harness):
            plan.say("ok", f"{harness} is a git checkout (left as it is: never pulled or reset)")
            return True
        what = ("a git checkout with no commit (an unfinished clone?)"
                if (harness / ".git").exists() else "not a git checkout")
        plan.say("FAILED", f"{harness} exists but is {what}: move it, then rerun")
        return False
    url = _origin()
    if not plan.proceed(f"git clone {url} {harness} (the folder the tick runs from)"):
        return False
    why = _clone(url, harness)
    plan.say("done" if why is None else "FAILED",
             f"cloned {url} to {harness}" if why is None else f"git clone {url}: {why}")
    return why is None


def _step_deps(plan):
    """The dependency folder the worker container mounts read-only: only the maintainer's deploy
    made it, and `capsule check` fails deps_mount without it. Empty is fine."""
    deps = plan.user / docker.DEPS_MOUNT
    if deps.is_dir():
        plan.say("ok", f"{deps} exists")
    elif plan.proceed(f"create {deps} (the folder the worker container mounts for dependencies)"):
        try:
            deps.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            plan.say("FAILED", f"create {deps}: {exc}")
        else:
            plan.say("done", f"created {deps}")


def _prefix_problem(plan):
    """Settle plan.prefix (CAPSULE_LAUNCHD_PREFIX wins, as in bin/lib_prefix.sh); return why it
    cannot be used, or None: another prefix already names Capsule jobs in ~/Library/LaunchAgents,
    and the scripts would look for this job under that one."""
    chosen = os.environ.get("CAPSULE_LAUNCHD_PREFIX")
    if chosen and not PREFIX_OK.fullmatch(chosen):
        return "CAPSULE_LAUNCHD_PREFIX may only use letters, digits, . and -"
    plan.prefix = chosen or plan.prefix
    agents = plan.user / "Library" / "LaunchAgents"
    others = sorted({m.group(1) for p in agents.glob("*.capsule-*.plist")
                     if (m := JOB_FILE.fullmatch(p.name)) and m.group(1) != plan.prefix})
    if others:
        return (f"other Capsule jobs here use prefix {others[0]}; set "
                f"CAPSULE_LAUNCHD_PREFIX={others[0]} or move them")
    return None


def _step_plist(plan, name):
    name_file = TEMPLATE.name.replace("__PREFIX__", plan.prefix).replace("__BOARD__", name)
    dest = plan.user / "Library" / "LaunchAgents" / name_file
    try:
        values = {**install.plist_values(plan), plists.BOARD: name, plists.PYTHON: sys.executable}
        text = plists.render(TEMPLATE.read_text(), values)
    except (OSError, ValueError) as exc:
        plan.say("FAILED", f"tick plist: {exc}")
        return False
    return install.write_plist(plan, dest, text, f"tick plist for {name}")


def run(args, parser):
    """The `board tick` handler: `args.rest` is [name]. Exit 0, or 1 on a refusal or FAILED."""
    if len(args.rest) != 1:
        parser.error("usage: board tick <name> [--dry-run] [--yes] [--capsule-home DIR]")
    name, plan = args.rest[0], install.Plan(args)
    problem = paths.board_name_problem(name)
    if not problem and not paths.config_path(plan.home, name).is_file():
        problem = (f"no board {name!r}: {paths.config_path(plan.home, name)} is missing "
                   "(create it with `capsule board new`)")
    if problem:
        print(f"capsule board tick: refused: {problem}", file=sys.stderr)
        return 1
    problem = _prefix_problem(plan)
    if problem:
        plan.say("FAILED", problem)      # before anything is written
        return 1
    install.step_home(plan)
    home_ok = not plan.failed
    _step_deps(plan)
    ready = _step_harness(plan)
    # a plist for a tick that has no folder to run from helps nobody (a dry run lists it anyway);
    # the start line is only printed once the plist on disk is the one this run would write
    if home_ok and (ready or plan.dry) and _step_plist(plan, name):
        flag, env = cli_ops.next_args(plan.home)
        if plan.prefix != install.DEFAULT_PREFIX:
            env = f"CAPSULE_LAUNCHD_PREFIX={shlex.quote(plan.prefix)} {env}"
        plan.say("next", "start it: " + cli_ops.START.format(name, flag, env))
    return 1 if plan.failed else 0
