"""`python3 -m capsule install` (public portable edition, P08): the stranger's one-command setup.

macOS only (anywhere else it prints a pointer and exits 2). Idempotent in bootstrap.sh's style:
every step prints one line, `ok` (already done), `done` (changed now), `WOULD` (dry run), `skip`
(left alone on purpose, the line says why or what to run), `next` (run this yourself) or `FAILED`.
A second run changes nothing and prints no `done` and no `WOULD` line. The exit code is doctor's,
or 1 when any step printed `FAILED`.

    capsule install --dry-run    print every step, change nothing (read-only probes only)
    capsule install --yes        take the defaults without asking
    capsule install              ask once per change

Steps: brew bundle (Brewfile; a cask whose tool is already installed is left out),
$CAPSULE_HOME, host.json (launchd.label_prefix, default
`dev.capsule`), the `capsule` launcher in ~/.local/bin, the keeper-watch plist in
~/Library/LaunchAgents (written, never loaded: the load command is printed), the worker image
(checked; the build command is printed), the per-board tick pointer, then `capsule doctor`.
Maintainer-only jobs and the dashboards are not part of this install and print nothing.

It never builds an image, never loads or bootstraps a launchd job (so it has no launchctl call),
never calls deploy.sh or bootstrap.sh, never edits a shell profile, and itself writes only under
$CAPSULE_HOME, ~/Library/LaunchAgents and ~/.local/bin (the tools doctor probes may create their
own files). It never reads or prints a secret.
"""
import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from capsule import cli_ops, docker_door, doctor, host, paths, plists, safe_write
from capsule.runners import docker

DEFAULT_PREFIX = "dev.capsule"
KEEPER = "capsule-corp-keeper-watch"
TEMPLATES = paths.REPO / "launchd" / "templates"
LINUX_POINTER = ("capsule install is macOS-only. On Linux run the harness by hand: see "
                 "docs/SETUP.md, section 'Linux, manual'.")
LAUNCHER_MARK = "# written by `capsule install`"
PROBE_SECONDS = 60
# cask -> its tool; the selected engine's cask joins it in _brewfile_without_installed_casks()
CASK_TOOLS = {"claude-code@latest": "claude"}
CASK_LINE = re.compile(r'^cask\s+"([^"]+)"')
# tests may set a Path to point the engine's app away; None = the selected engine's app_path
DOCKER_APP = None


def add_parser(sub):
    p = sub.add_parser("install", help="macOS setup: brew bundle, CAPSULE_HOME, host.json, the"
                                       " capsule launcher, the keeper-watch plist; ends with"
                                       " `capsule doctor`")
    p.add_argument("--dry-run", action="store_true",
                   help="print every step and change nothing")
    p.add_argument("--yes", action="store_true", help="take the defaults without asking")
    p.add_argument("--capsule-home", default=None,
                   help="where the factory keeps its state (default: ~/.local/share/capsule)")


class Plan:
    """What one run needs: the folders, the label prefix, the mode and the output."""

    def __init__(self, args):
        self.dry, self.yes = args.dry_run, args.yes
        self.home = paths.home(args.capsule_home).absolute()  # a clone runs from the checkout
        self.user = Path.home()
        self.failed = False
        try:
            chosen = host.get("launchd.label_prefix", self.home)
        except host.HostError:
            chosen = None       # the host.json step reports the refusal
        self.prefix = chosen or DEFAULT_PREFIX

    def say(self, status, text):
        print(f"{status} {text}")
        self.failed = self.failed or status == "FAILED"

    def proceed(self, what):
        """True when `what` may be done now: never on a dry run, always with --yes, else only
        when the person types y (no answer, or no terminal, is no)."""
        if self.dry:
            self.say("WOULD", what)
            return False
        if self.yes:
            return True
        try:
            answer = input(f"{what}? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() in ("y", "yes"):
            return True
        self.say("skip", f"{what} (declined)")
        return False

    def write(self, root, path, text, what, mode=0o644):
        """Write `text` to `path` under `root` (never through a symlink) and say so."""
        try:
            root.mkdir(parents=True, exist_ok=True)
            safe_write.atomic_write(root, path, text, create_dirs=True, mode=mode)
        except OSError as exc:
            self.say("FAILED", f"{what}: {exc}")
        else:
            self.say("done", f"wrote {path}")


def _brew(brewfile, *args, capture=True, timeout=None):
    """`brew <args>` with `brewfile` (text) on stdin, no auto-update and no upgrade of tools the
    person already has (an outdated formula would otherwise make every rerun upgrade it); output is
    streamed to the terminal unless `capture`. A brew that cannot run is a finished process with
    code 127."""
    cmd = ["brew", *args]
    env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_BUNDLE_NO_UPGRADE": "1"}
    try:
        return subprocess.run(cmd, input=brewfile, text=True, env=env, capture_output=capture,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _read(path):
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _brewfile_without_installed_casks(plan, brewfile):
    """The Brewfile text minus each cask whose tool doctor already finds, or whose app is in
    /Applications (an app installed outside brew makes `brew bundle` fail on that cask every
    run); one `skip` line per cask."""
    kept = []
    eng = docker_door.docker_engine_row()
    tools = {**CASK_TOOLS, eng["cask"]: "docker"}
    for line in brewfile.read_text().splitlines():
        cask = CASK_LINE.match(line)
        tool = tools.get(cask.group(1)) if cask else None
        if tool and (doctor._tool(tool, "") is None
                     or (tool == "docker" and Path(DOCKER_APP or eng["app_path"]).exists())):
            plan.say("skip", f"cask {cask.group(1)}: {tool} is already installed")
        else:
            kept.append(line)
    return "\n".join(kept) + "\n"


def _step_brew(plan):
    brewfile = paths.REPO / "Brewfile"
    if not shutil.which("brew"):
        plan.say("FAILED", "Homebrew not found: install it from https://brew.sh, then rerun")
        return
    text = _brewfile_without_installed_casks(plan, brewfile)
    check = _brew(text, "bundle", "check", "--file=-", timeout=PROBE_SECONDS)
    if check.returncode == 0:
        plan.say("ok", "Brewfile packages installed")
    elif plan.proceed("brew bundle (the Brewfile minus the skipped casks)"):
        done = _brew(text, "bundle", "--file=-", capture=False)
        plan.say("done" if done.returncode == 0 else "FAILED",
                 "brew bundle" if done.returncode == 0 else
                 f"brew bundle exited {done.returncode}: run it yourself to see why")


def step_home(plan):
    missing = [d for d in (plan.home, plan.home / "logs") if not d.is_dir()]
    if not missing:
        plan.say("ok", f"{plan.home} and its logs folder exist")
    elif plan.proceed(f"create {plan.home} and its logs folder"):
        try:
            (plan.home / "logs").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            plan.say("FAILED", f"create {plan.home}: {exc}")
        else:
            plan.say("done", f"created {plan.home}")


def _step_host(plan):
    level, _, detail = host.check_row(plan.home)
    path = plan.home / "host.json"
    if level != "ok":
        plan.say("FAILED", f"host.json: {detail} (edit it; the installer never overwrites it)")
    elif path.is_file():
        plan.say("ok", f"{path} valid")
    elif plan.proceed(f"write {path} (launchd.label_prefix={plan.prefix})"):
        text = json.dumps({"launchd": {"label_prefix": plan.prefix}}, indent=2) + "\n"
        plan.write(plan.home, path, text, "host.json")


def _launcher_text(plan):
    """The launcher script. `-P` keeps the current folder off sys.path (a `capsule/` folder
    there must never shadow this clone); a non-default home is carried unless the caller sets it."""
    home = ""
    if plan.home != plan.user / ".local" / "share" / "capsule":
        home = f"CAPSULE_HOME=${{CAPSULE_HOME:-{shlex.quote(str(plan.home))}}} "
    return (f"#!/bin/sh\n{LAUNCHER_MARK}: runs the capsule CLI from {paths.REPO}\n"
            f"{home}PYTHONPATH={shlex.quote(str(paths.REPO))}${{PYTHONPATH:+:$PYTHONPATH}} "
            f"exec {shlex.quote(sys.executable)} -P -m capsule \"$@\"\n")


def _step_launcher(plan):
    dest = plan.user / ".local" / "bin" / "capsule"
    want, have = _launcher_text(plan), _read(dest)
    if have == want:
        plan.say("ok", f"{dest} launcher current")
    elif have is not None and LAUNCHER_MARK not in have:
        plan.say("skip", f"{dest} exists and is not ours: move it, then rerun")
    elif plan.proceed(f"write {dest} (the `capsule` launcher)"):
        plan.write(plan.user, dest, want, "launcher", mode=0o755)
    if str(dest.parent) not in os.environ.get("PATH", "").split(":"):
        plan.say("skip", f"{dest.parent} is not on PATH: add this line to your shell profile "
                         f"yourself: export PATH=\"$HOME/.local/bin:$PATH\"")


def plist_values(plan):
    """The four values every plist template is filled with (see plists.PLACEHOLDERS)."""
    return {"__PREFIX__": plan.prefix, "__HOME__": str(plan.user),
            "__REPO__": str(paths.REPO), "__CAPSULE_HOME__": str(plan.home)}


def write_plist(plan, dest, text, what):
    """Write the rendered plist `text` to `dest` under the user's home, written never loaded. A
    differing file or a symlink is left alone (`skip`), a current one is `ok`. True when `dest`
    holds `text` afterwards."""
    if dest.is_symlink() or (dest.exists() and _read(dest) != text):
        plan.say("skip", f"{dest} differs: move it, then rerun")
        return False
    if dest.exists():
        plan.say("ok", f"{dest} current")
    elif plan.proceed(f"write {dest} ({what}; written, not loaded)"):
        plan.write(plan.user, dest, text, what)
    return dest.is_file()


def _step_keeper(plan):
    template = TEMPLATES / f"__PREFIX__.{KEEPER}.plist"
    dest = plan.user / "Library" / "LaunchAgents" / template.name.replace("__PREFIX__", plan.prefix)
    try:
        text = plists.render(template.read_text(), plist_values(plan))
    except (OSError, ValueError) as exc:
        plan.say("FAILED", f"keeper-watch plist: {exc}")
        return
    write_plist(plan, dest, text, "keeper-watch plist")
    if not dest.is_symlink() and _read(dest) == text:
        label = dest.stem
        plan.say("skip", f"{label} is installed, not loaded; to load it: "
                         f"launchctl bootstrap gui/{os.getuid()} {shlex.quote(str(dest))}")


def _step_boards(plan):
    plan.say("skip", "per-board tick plists and the harness checkout (set up per board: run "
                     f"{cli_ops.LAUNCHER} board tick <name>)")


def _step_image(plan):
    found = doctor.found_image(plan.home)
    if found:
        plan.say("ok", f"worker image present ({found[0]})")
    else:
        plan.say("next", f"worker image not built: run {docker.build_command(plan.home)} "
                         f"({docker.BUILD_NOTE}; it records the image in "
                         f"{paths.image_record(plan.home)})")


STEPS = (_step_brew, step_home, _step_host, _step_launcher, _step_keeper, _step_boards,
         _step_image)


def handle(args, parser):
    if platform.system() != "Darwin":
        print(LINUX_POINTER, file=sys.stderr)
        return 2
    plan = Plan(args)
    for step in STEPS:
        step(plan)
    if plan.dry:
        plan.say("WOULD", "capsule doctor (read-only check of every prerequisite)")
        return 1 if plan.failed else 0
    ours = argparse.Namespace(offline=False, capsule_home=str(plan.home))
    return 1 if doctor.handle(ours, None) or plan.failed else 0
