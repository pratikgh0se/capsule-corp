"""`python3 -m capsule doctor` (public portable edition, P07): the read-only first-run check.

Unlike `capsule check --board`, it needs no board. One line per prerequisite, `ok <what>` or
`MISSING <what> -> <the command to run or the thing to install>`, then the effective host.json
(`host.check_row`, every value with its source). Exit 1 when any line is MISSING. `run()` is the
reusable list (P08's install.sh ends by calling `capsule doctor`).

Read-only: it never installs, writes, starts a service or reads a secret. The only network call
is `gh auth status`; `--offline` skips it. `launchctl` is checked by presence only (running it
would touch launchd). Tools are found on PATH; docker goes through the one guarded door.
"""
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from functools import partial
from pathlib import Path

from capsule import config, docker_door, host, paths, threat_model
from capsule.runners import docker

MIN_PYTHON, MIN_MACOS, MIN_FREE_BYTES = (3, 11), 14, 25 * 10**9   # 25 GB: always-free floor, D10
# 40 GB is a different figure for a different job: building the image (D106). It is never typed
# here; BUILD_FREE_BYTES below reads it from docker/BUILD_MIN_FREE_BYTES.
# what build-base.sh refuses under, asked for while no image: the file it reads, via the docker
# runner; None when the file is missing or not a whole number (the disk check then fails)
BUILD_FREE_BYTES = docker.build_floor()
MIN_GIT = (2, 40)
SWAP_WARN_GB, SWAP_FAIL_GB = 8, 20   # used swap: warn at 8, fail at 20 (8 if disk is low)
_GB = {"K": 1 / 1024**2, "M": 1 / 1024, "G": 1.0}
PROBE_SECONDS = 20
STACK_DOCKERFILE = Path("docker") / "stacks" / "python" / "Dockerfile"
BUILD_INPUTS = (Path("docker") / "build-base.sh", Path("docker") / "base" / "Dockerfile",
                STACK_DOCKERFILE, Path("docker") / "BUILD_MIN_FREE_BYTES")


def add_parser(sub):
    p = sub.add_parser("doctor", help="read-only first-run check of every prerequisite: one"
                                      " line each, exit 1 with a fix line when one is missing",
                       description="Checks every Capsule prerequisite (tools, logins, images,"
                                   " folders), one line each; read-only.")
    p.add_argument("--offline", action="store_true",
                   help="skip the one network check (gh auth status)")
    p.add_argument("--capsule-home", default=None)
    p.add_argument("--repo", default=None,
                   help="a repo on the factory: warn when its docs/threat-model.md is not valid")


def _probe(*cmd):
    """The finished process of a read-only tool call, or None when it cannot run. A timeout
    still raises subprocess.TimeoutExpired: it needs a different fix than a failed login."""
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, timeout=PROBE_SECONDS)
    except subprocess.TimeoutExpired:
        raise
    except (OSError, subprocess.SubprocessError):
        return None


def _macos():
    if platform.system() != "Darwin":
        return "use a Mac on macOS 14 or newer (the installer is macOS-only; Linux runs by hand)"
    version = platform.mac_ver()[0]
    major = int(version.split(".")[0] or 0)
    return None if major >= MIN_MACOS else f"update macOS to {MIN_MACOS}+ (this is {version})"


def _python():
    if sys.version_info[:2] >= MIN_PYTHON:
        return None
    want = ".".join(map(str, MIN_PYTHON))
    return f"install Python {want}+: brew install uv && uv python install {want}"


def _tool(name, install):
    return None if shutil.which(name) else install


def _docker():
    args = ["version", "--format", "{{.Server.Version}}"]
    eng = docker_door.docker_engine_row()
    app, hint = eng["title"], eng["open_hint"]
    try:
        done = docker_door.docker_run(args, capture_output=True, text=True, timeout=PROBE_SECONDS)
    except subprocess.TimeoutExpired:
        return f"Docker is not responding: restart {app} ({hint}), then rerun"
    except (OSError, subprocess.SubprocessError):
        return f"install {app}: brew install --cask {eng['cask']}, then {hint}"
    return None if done.returncode == 0 else f"start {app}: {hint}, then rerun"


def found_image(home):
    """Where the worker image is here, as `(ref, where)`, or None: the runner's own resolution
    (`docker.worker_image_source`: the operator's pins, else the capsule-python row of
    $CAPSULE_HOME/images.lock that build-base.sh wrote), so doctor is never green while the runner
    refuses. No image, a bad row or no such image: not found."""
    image, source = docker.worker_image_source(home)
    how = "pinned by OPS_WORKER_IMAGE" if source.endswith("OPS_WORKER_IMAGE") else \
        f"recorded in {paths.image_record(home)}"
    try:
        done = docker_door.docker_run(["image", "inspect", image], capture_output=True,
                                      text=True, timeout=PROBE_SECONDS) if image else None
    except (OSError, subprocess.SubprocessError):
        done = None
    return (image, how) if done is not None and done.returncode == 0 else None


def _image_row(home, found):
    """(what, fix) of the worker image line, which says which of three it is: present; absent but
    buildable here (met: the line carries the build command); absent and not buildable (the
    Dockerfiles or build-base.sh are gone from this clone)."""
    if found:
        return f"worker image present ({found[1]})", None
    gone = [str(p) for p in BUILD_INPUTS if not (paths.REPO / p).is_file()]
    if gone:
        return ("worker image buildable (docker/build-base.sh and its Dockerfiles)",
                f"restore {', '.join(gone)} from the repo (git checkout -- docker)")
    return (f"worker image not built yet, buildable here: build it with "
            f"{docker.build_command(home)} ({docker.BUILD_NOTE})"), None


def _claude():
    if not shutil.which("claude"):
        return "brew install --cask claude-code@latest"
    done = _probe("claude", "auth", "status")
    try:
        logged_in = done is not None and json.loads(done.stdout).get("loggedIn") is True
    except (ValueError, AttributeError):
        logged_in = False
    return None if logged_in else "log in with your Claude subscription: run claude, then /login"


def _gh():
    if not shutil.which("gh"):
        return "brew install gh"
    try:
        done = _probe("gh", "auth", "status")
    except subprocess.TimeoutExpired:
        return "gh auth status timed out (offline?): rerun with --offline to skip the login check"
    return None if done is not None and done.returncode == 0 else "log in to GitHub: gh auth login"


def _git():
    """git is there and is 2.40+: `protected_scope` needs `--attr-source`, and older git falls
    back to flagging main's merges."""
    if not shutil.which("git"):
        return "brew install git"
    done = _probe("git", "--version")
    found = re.search(r"(\d+)\.(\d+)[\d.]*", done.stdout if done else "")
    if found and tuple(map(int, found.groups())) >= MIN_GIT:
        return None
    seen = found.group(0) if found else "unreadable"
    return f"update git to {MIN_GIT[0]}.{MIN_GIT[1]}+ (this is {seen}): brew upgrade git"


def _existing(home):
    """`home`, or its nearest parent that exists (doctor never creates the folder)."""
    probe = Path(home)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _home_writable(home):
    probe = _existing(home)
    if probe.is_dir() and os.access(probe, os.W_OK | os.X_OK):
        return None
    return f"make it writable: chmod u+w {shlex.quote(str(probe))}, or point CAPSULE_HOME elsewhere"


def _disk(home, need):
    if need is None:
        return f"restore {docker.BUILD_FLOOR_FILE}: it must hold the disk floor in bytes"
    free = shutil.disk_usage(_existing(home)).free
    if free >= need:
        return None
    short_gb = -(-(need - free) // 10**9)
    return f"free {short_gb} GB more on the volume holding {home}"


def _host_json(level, detail):
    return None if level == "ok" else f"edit it so it parses ({detail})"


def _swap_gb(text):
    """Used swap in GB from `sysctl -n vm.swapusage` ("... used = 5321.12M ..."), or None."""
    found = re.search(r"used\s*=\s*(\d+(?:\.\d+)?)([KMG])", text or "")
    return float(found.group(1)) * _GB[found.group(2)] if found else None


def _top_process(text):
    """(name, size text) of the first row under the COMMAND/MEM header of `top -l 1 -o mem -n 1
    -stats command,mem`, or None."""
    rows = (text or "").split("COMMAND", 1)[-1].splitlines()[1:]
    for row in rows:
        found = re.match(r"\s*(.+?)\s+(\d+(?:\.\d+)?[KMG])[+-]?\s*$", row)
        if found:
            return found.group(1), found.group(2)
    return None


def swap_check():
    """(level, one plain line) for used swap: "ok", "warn" or "fail", or None when skipped (not
    a Mac, or sysctl/top gave nothing readable). Warn from 8 GB used; fail from 20 GB, or from
    8 GB while free disk on / is under 25 GB (D10). Names the biggest memory process; fseventsd
    gets the fix. Only reports: it never kills or signals anything."""
    if platform.system() != "Darwin":
        return None
    try:
        done = _probe("sysctl", "-n", "vm.swapusage")
    except subprocess.TimeoutExpired:
        return None
    used = _swap_gb(done.stdout if done and done.returncode == 0 else "")
    if used is None:
        return None
    low_disk = shutil.disk_usage("/").free < MIN_FREE_BYTES
    if used < SWAP_WARN_GB:
        return "ok", f"swap use is {used:.1f} GB"
    level = "fail" if used >= SWAP_FAIL_GB or low_disk else "warn"
    line = f"swap use is {used:.1f} GB" + (" and the disk is under 25 GB free" if low_disk else "")
    try:
        top = _probe("top", "-l", "1", "-o", "mem", "-n", "1", "-stats", "command,mem")
    except subprocess.TimeoutExpired:
        top = None
    big = _top_process(top.stdout if top and top.returncode == 0 else "")
    if big and big[0] == "fseventsd":
        line += (f"; fseventsd holds {big[1]}: in a normal terminal run `sudo killall fseventsd`"
                 " (macOS restarts it; Spotlight may re-index)")
    elif big:
        line += f"; the biggest memory user is {big[0]} at {big[1]}"
    return level, line


def _guarded(check):
    """`check()`, except that a check which raises becomes a one-line fix so the rest still run."""
    try:
        return check()
    except Exception as exc:    # noqa: BLE001 - any failure of one check is that check's finding
        return f"could not check ({type(exc).__name__}: {' '.join(str(exc).split())[:100]})"


def run(home=None, offline=False, host_row=None, swap=None):
    """[(what, fix)] for every prerequisite; fix is None when it is met, else one line saying
    what to run or install. Reads only; `offline` skips the gh login call. `host_row` is
    `host.check_row(home)` when the caller already has it; `swap` likewise for `swap_check()`. A
    failing swap check is a row; a warning is printed by `handle`, it does not fail the run."""
    home = paths.home(home)
    level, _, detail = host_row or host.check_row(home)
    gh = ("gh installed (login not checked: --offline)", partial(_tool, "gh", "brew install gh")) \
        if offline else ("gh installed and logged in (gh auth status)", _gh)
    found = found_image(home)
    image_what, image_fix = _image_row(home, found)
    need = MIN_FREE_BYTES if found else BUILD_FREE_BYTES   # absent: the build's own floor
    checks = [
        (f"macOS {MIN_MACOS}+", _macos),
        (f"Python {'.'.join(map(str, MIN_PYTHON))}+", _python),
        ("Homebrew", partial(_tool, "brew", "install Homebrew from https://brew.sh")),
        ("git 2.40+", _git),
        ("Docker daemon running", _docker),
        (image_what, lambda: image_fix),
        ("claude installed and logged in", _claude),
        gh,
        ("launchctl present", partial(_tool, "launchctl",
                                      "run Capsule on a Mac: launchctl ships with macOS")),
        (f"CAPSULE_HOME writable ({home})", partial(_home_writable, home)),
        (f"free disk {need // 10**9} GB" if need else "free disk (build floor unreadable)",
         partial(_disk, home, need)),
        ("host.json valid", partial(_host_json, level, detail)),
    ]
    swap = _guarded(swap_check) if swap is None else swap
    rows = [(what, _guarded(check)) for what, check in checks]
    if isinstance(swap, str):      # the check itself broke: report it like any other
        rows.append(("swap check", swap))
    elif swap and swap[0] == "fail":
        rows.append(("swap use", swap[1]))
    return rows


def handle(args, parser):
    home = paths.home(args.capsule_home)
    host_row = host.check_row(home)
    swap = _guarded(swap_check)
    rows = run(home, args.offline, host_row, swap)
    for what, fix in rows:
        print(f"ok {what}" if fix is None else f"MISSING {what} -> {fix}")
    if isinstance(swap, tuple) and swap[0] == "warn":
        print(f"warning {swap[1]}")
    elif isinstance(swap, tuple) and swap[0] == "ok":
        print(f"ok {swap[1]}")
    if host_row[0] == "ok":
        parts = [host.describe(k, v, src) for k, v, src in host.effective(home)]   # paste-safe
        print(f"host.json settings: {'; '.join(parts)}")
    for cfg in sorted((home / "boards").glob("*/config.json")):   # a retired image_loop value
        try:
            loop = (json.loads(cfg.read_text(encoding="utf-8")).get("redesign") or {})["image_loop"]
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            continue
        if config.legacy_image_loop(loop):
            print(f"warning: board {cfg.parent.name!r}: redesign.image_loop {loop!r} is retired "
                  "and ignored (the image built here is always used); remove it")
    missing = sum(fix is not None for _, fix in rows)
    repo = getattr(args, "repo", None)
    for problem in threat_model.check(repo) if repo is not None else ():   # a warning only
        print(f"warning {problem}")
    print(f"doctor: {missing} missing" if missing else "doctor: all prerequisites met")
    return 1 if missing else 0
