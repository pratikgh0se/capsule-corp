"""The Mac lane (D305, implements D167): a card whose runner tier is `mac` runs its shell under
a seatbelt profile that is the light tier's plus read of the selected Xcode and no network
(`seatbelt.render` with a `developer_dir`; `endpoints`, which nothing passes yet, would be the
only way out), with Xcode's developer dir, the SDK and every cache set up by `exec_mcp`'s
`--developer-dir` mode. Never docker, never the light tier: a Mac card the lane cannot run is
held with a reason (`unavailable_reason`), never moved. A mac card's own work (the card's
`check_cmd`, `host_only` commands, the blind interface's checks and its blind suite
`run_blind`) runs through `session`, on the host under the same profile. The board's other
contract commands run in the project container as for any card.

One Mac job at a time per Capsule home: `mac-lane.lock` is taken (`acquire`, or `lane` to wait
for it) before a mac run is claimed and handed to the `run-one` process that outlives the tick
(`TickRuns._mac_lane`), so the kernel frees it when that process ends or dies and no stale lock
can block the lane. A flow holds it once: whoever takes it passes the handle down (`session`'s
`lock`) and closes it itself.
"""
import contextlib
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

from capsule import paths, tick_guard
from capsule.runners import host, sandbox, seatbelt

LOCK_NAME = "mac-lane.lock"
_PYTHON = shlex.quote(os.path.realpath(sys.executable))     # read root: `sys.base_prefix`
LANE_POLL_S = 2             # a run waiting for the lane looks again this often

OFF = ("The Mac lane is switched off for this board. Turn on redesign.mac_runner in the board"
       " settings to run Mac cards.")
NOT_MACOS = "The Mac lane needs a Mac. This host is not running macOS."
NO_SANDBOX = "The Mac lane needs macOS sandbox-exec, which was not found."
NO_XCODE = "The Mac lane needs Xcode with the Swift toolchain. None was found."
BUSY = "The Mac lane is busy with another job."


class LaneWait(str):
    """A lane reason (`OFF`, `BUSY`, ...) as a check's structured wait: only `mac` makes one, so
    a gate whose own output holds the same words is never mistaken for a lane wait."""


def acquire(home=None):
    """The open, exclusively locked `mac-lane.lock` handle, or None while another mac run holds
    it. The lock lives as long as an open handle to it does, in this process or an inheritor."""
    return tick_guard._try_acquire(Path(paths.home(home)) / LOCK_NAME)


@contextlib.contextmanager
def lane(home=None, stopped=None):
    """Hold `mac-lane.lock` for the block: yields the handle, or None. `stopped` None tries once
    (None while another mac run holds it); an Event waits for the lane, looking every
    `LANE_POLL_S`, and yields None only once the event is set (the run was cancelled)."""
    fh = acquire(home)
    while fh is None and stopped is not None and not stopped.wait(LANE_POLL_S):
        fh = acquire(home)
    try:
        yield fh
    finally:
        if fh is not None:
            fh.close()


def busy(home=None):
    """True while another mac run holds the lane (a look: the lock is not kept)."""
    fh = acquire(home)
    if fh is not None:
        fh.close()
    return fh is None


def unavailable(enabled=True):
    """Why the lane cannot run a card now (`MacRunner.unavailable_reason`; `enabled` is the board's
    switch), or None."""
    runner = MacRunner()
    runner.enabled = enabled
    return runner.unavailable_reason()


def _run(*argv):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _probe():
    """(reason, developer dir): NO_XCODE unless the selected developer dir is a full Xcode, not
    just the Command Line Tools, and `xcrun --find swift` works. Asked afresh every call (no
    cache: a fixed cause is seen on the next tick, B3.9); the tick calls it once per pass.
    `xcrun` is not run without a full Xcode: on a Mac with no developer tools it would offer
    to install them."""
    selected = _run("xcode-select", "-p")
    try:        # the shape `seatbelt.render` needs: a full Xcode app, links resolved
        seatbelt._xcode_contents(os.path.realpath(selected or "/"))
    except ValueError:
        return NO_XCODE, None
    return (None, selected) if _run("xcrun", "--find", "swift") else (NO_XCODE, None)


class MacRunner(host.LightRunner):
    """Mac lane: the light runner's start/limits/reap with the Mac profile and the Mac exec
    environment. Off unless the board's `redesign.mac_runner` switch is on (`runners.scoped`)."""

    switch = "mac_runner"
    tier = "mac"

    def unavailable_reason(self):
        """Why the lane cannot run a card now (contract B3, first that applies), or None."""
        if not self.enabled:
            return OFF
        if platform.system() != "Darwin":
            return NOT_MACOS
        if not os.path.exists(host.SANDBOX_EXEC):
            return NO_SANDBOX
        return _probe()[0]

    def available(self):
        return self.unavailable_reason() is None

    def start(self, run_id, workspace_dir, requests_dir=None, git_dir=None, tmpdir=None, read=()):
        """The light runner's start, with this run's Xcode resolved once here: the profile,
        `--developer-dir` and `--sdk-root` all use it, so they cannot disagree. The SDK path is
        asked of `xcrun` here, on the host, outside the sandbox. `read`: further read-only paths."""
        dev = _probe()[1]
        sdk = _run("xcrun", "--show-sdk-path") if dev else None
        if not sdk:     # `available()` said yes a moment ago: never run without Xcode
            raise RuntimeError(NO_XCODE)
        profile = seatbelt.render(workspace_dir, [p for p in (git_dir, *read) if p],
                                  developer_dir=dev)
        return super().start(run_id, workspace_dir, requests_dir, git_dir, tmpdir, profile=profile,
                             server_args=["--developer-dir", dev, "--sdk-root", sdk])


HOST_TOOLS = frozenset(("xcodebuild", "xcrun", "swift", "swiftc", "codesign", "simctl", "plutil"))


def host_only(cmd, cfg):
    """True when a mac card's contract command must run on the host: the card's `check_cmd`, a
    command whose first word (after leading `VAR=value` words) is an Apple toolchain tool
    (`HOST_TOOLS`), or one that starts with a prefix in the board's `mac.host_cmds`. Nothing else
    is guessed: every other command runs in the project container, as for any card."""
    from capsule.check_gate import CheckCmd
    if isinstance(cmd, CheckCmd):
        return True
    text = str(cmd).strip()
    if any(text.startswith(str(p)) for p in ((cfg or {}).get("mac") or {}).get("host_cmds") or ()):
        return True
    try:
        words = [w for w in shlex.split(text) if not re.fullmatch(r"[A-Za-z_]\w*=.*", w)]
    except ValueError:
        return False
    return bool(words) and words[0] in HOST_TOOLS


@contextlib.contextmanager
def _split(src, cfg, seconds, runner, home):
    """`checks` for a mac card: one `run` that sends a `host_only` command to the host session
    (`run.lock`, `run.lane` and `run.wt` are that session's) and every other command to the
    board's container session."""
    from capsule import config
    with (session(src, home, enabled=config.redesign(cfg)["mac_runner"]) as host_run,
          sandbox.session(src, cfg, seconds, runner, home) as box_run):
        if host_run is None:
            yield None
            return

        def run(cmd, timeout):
            return (host_run if host_only(cmd, cfg) else box_run)(cmd, timeout)
        run.lock, run.lane, run.wt = host_run.lock, host_run.lane, host_run.wt
        yield run


def checks(src, cfg, seconds, runner=None, home=None, mac_card=False):
    """The session the lander's checks of a card run in: `sandbox.session` (docker), or for a
    `mac_card` `_split` (host-only commands on the Mac lane, the rest in docker)."""
    if mac_card:
        return _split(src, cfg, seconds, runner, home)
    return sandbox.session(src, cfg, seconds, runner, home)


def card_is_mac(conn, task_id):
    """True when the card's runner tier is `mac` (False without a board or a card)."""
    row = conn.execute("SELECT tier FROM tasks WHERE id=?", (task_id,)).fetchone() if (
        conn is not None and task_id) else None
    return row is not None and row[0] == "mac"


@contextlib.contextmanager
def session(src, home=None, *, enabled=True, lock=None, git_dir=None, read=(), copy=True):
    """Yield `run(cmd, timeout) -> (rc, output)`: a mac card's command on the host, never in
    docker. It runs under the Mac profile in a scratch copy of the tree `src` (None: yields None),
    so `src` is never written, with no network, inside the lane's one-job lock. `run.wt` is the
    copy. `lock` is a handle the caller already holds (it keeps closing it); else the lock is
    taken here, and a lane that cannot run the card (`unavailable_reason`, `enabled` the board's
    switch) or that is busy makes `run` return `(None, why)` and nothing runs: the caller waits,
    it never fails the card. `git_dir` (a clone's, outside `src`) and `read` are read-only; a
    timeout is `(124, ...)`. `copy=False` (the blind suite): no copy, an empty writable box, and
    `src` itself is granted read-only, so the suite cannot write the product."""
    if src is None:
        yield None
        return
    own = lock is None
    with contextlib.ExitStack() as stack:
        why = unavailable(enabled) if own else None
        if own and not why:
            lock = stack.enter_context(lane(home))
            why = None if lock else BUSY
        box = Path(tempfile.mkdtemp(prefix="mac-box-"))
        wt = os.path.realpath(box / "w")
        stack.callback(shutil.rmtree, box, ignore_errors=True)
        stack.callback(host._kill_under, wt)
        run = None if why else _start_run(own, box, wt, (src, git_dir, read, copy, lock))
        yield run if callable(run) else _waiting(run or why)


def _start_run(own, box, wt, args):
    """`_boxed_run`, or for a lane that is ours its failure to start (no Xcode after all) as the
    reason text; a caller's own lane raises it."""
    try:
        return _boxed_run(box, wt, *args)
    except RuntimeError as exc:
        if not own:
            raise
        return str(exc)


def _waiting(why):
    """A `run` that runs nothing: `(None, why)`, `why` a `LaneWait`; `run.lane` says so, and
    `run.lock` is the lane lock a real run holds (here None)."""
    def run(cmd, timeout):
        return None, run.lane
    run.wt, run.lock, run.lane = None, None, LaneWait(why)
    return run


def _grantable(path):
    """False for a read grant that would cover `/`, the home dir or a parent of it."""
    home, path = os.path.realpath(Path.home()), os.path.realpath(path)
    return path != "/" and home != path and not home.startswith(path.rstrip("/") + "/")


def _copy_tree(src, wt):
    """`src` copied to `wt`: regular files, directories and symlinks (kept as links); FIFOs,
    sockets and devices are skipped, the top-level `.worker-exec` (an earlier run's HOME and
    caches) is left out, and an unreadable file is skipped with a note on stderr."""
    top = os.path.realpath(src)

    def ignore(here, names):
        skip = {".worker-exec"} if os.path.realpath(here) == top else set()
        for name in names:
            try:
                mode = os.lstat(os.path.join(here, name)).st_mode
            except OSError:
                skip.add(name)
                continue
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                skip.add(name)
        return skip
    try:
        shutil.copytree(src, wt, symlinks=True, ignore=ignore)
    except shutil.Error as exc:
        print(f"mac lane: not copied (unreadable): {len(exc.args[0])} path(s)", file=sys.stderr)


def _point_git(wt, git_dir):
    """`wt`'s `.git` made a pointer file to `git_dir`, never written through a link."""
    path = os.path.join(wt, ".git")
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"gitdir: {git_dir}\n")


def _ruff():
    """The ruff binary the trusted gates (`tests/size_check.py`: ruff is a required oracle) can
    run in the sandbox, found on the host, per session: `ruff` on PATH, else the pinned
    `uvx --offline` environment's. None when there is none."""
    found = shutil.which("ruff")
    if found:
        return os.path.realpath(found)
    pin = re.search(r'RUFF_VERSION = "([^"]+)"', (paths.REPO / "tests/size_check.py").read_text())
    uvx = shutil.which("uvx")
    prefix = pin and uvx and _run(uvx, "--offline", "--from", f"ruff@{pin[1]}", "python", "-c",
                                  "import sys; print(sys.prefix)")
    ruff = os.path.join(os.path.realpath(prefix), "bin", "ruff") if prefix else None
    return ruff if ruff and os.path.isfile(ruff) else None


def _host_paths(cmd, wt, git_dir, ruff=None):
    """`cmd` with the docker form's paths mapped to this session's host paths, in one pass:
    `/trusted/tests` -> the trusted checkout's tests (read-only), `/workspace` -> the scratch
    copy `wt`, `/.gitdirs/<name>` -> the real git dir (read-only). A command that named any of
    them was written for the worker image, whose `python3` and `ruff` are the capsule's own: it
    gets this process's interpreter (`_PYTHON`) and the host's `ruff` (the binary's dir on PATH),
    as the sandbox's system `python3` is too old for the repo and has no ruff."""
    table = {f"{sandbox.TRUSTED}/tests": str(paths.REPO / "tests"), sandbox.WORKSPACE: wt}
    if git_dir:
        table[f"/.gitdirs/{os.path.basename(git_dir)}"] = str(git_dir)
    names = "|".join(re.escape(k) for k in sorted(table, key=len, reverse=True))
    cmd, hit = re.subn(rf"(?<![\w./-])({names})(?![\w.-])", lambda m: shlex.quote(table[m[1]]), cmd)
    if not hit:
        return cmd
    cmd = re.sub(r"(?<![\w./-])python3(?![\w.-])", lambda _: _PYTHON, cmd)
    return f'export PATH={shlex.quote(os.path.dirname(ruff))}:"$PATH"; {cmd}' if ruff else cmd


JUDGES = ("ui_timing.py", "ui_motion.py")


def _judge_dir(box):
    """A folder in `box`, beside the worktree and so outside its write grant, with a copy of each
    UI-proof judge that exists; None when none exists. The session reads it (never writes it)."""
    here = paths.REPO / "capsule"
    keep = [here / name for name in JUDGES if (here / name).is_file()]
    if not keep:
        return None
    folder = box / "ui-proof"
    folder.mkdir()
    for path in keep:
        shutil.copyfile(path, folder / path.name)
        os.chmod(folder / path.name, 0o444)
    return os.path.realpath(folder)


def _boxed_run(box, wt, src, git_dir, read, copy, lock):
    """`session`'s `run`: `src` copied to `wt` inside `box`, its profile written beside it. A
    copy's commands may name the docker form's paths (`_host_paths`). `run.lock` is the lane lock
    the session holds: a check that runs inside it passes it on, never taking the lane twice."""
    from capsule.runners import exec_mcp
    ruff = judges = None
    if copy:
        _copy_tree(src, wt)
        ruff = _ruff()
        judges = _judge_dir(box)
        read = (*read, str(paths.REPO / "tests"), os.path.realpath(sys.base_prefix), ruff, judges)
    else:
        os.mkdir(wt)
        read = (*read, src)
    read = [p for p in read if p and _grantable(p)]
    if git_dir:             # the clone's pointer is relative: aim the copy at the real one
        _point_git(wt, git_dir)
    runner = MacRunner()
    # (worktree, profile, "--developer-dir", dir, "--sdk-root", sdk)
    _, profile, _, dev, _, sdk = runner._runs[runner.start("box", wt, git_dir=git_dir, read=read)]
    (box / "profile.sb").write_text(profile, encoding="utf-8")
    cfg = types.SimpleNamespace(sandbox_profile=str(box / "profile.sb"), workspace=wt,
                                dry_run=False, developer_dir=dev, sdk_root=sdk)

    def run(cmd, timeout):
        cmd = _host_paths(cmd, wt, git_dir, ruff) if copy else cmd
        if judges:      # CAPSULE_UI_PROOF: the read-only folder of the UI-proof judges
            cmd = f"export CAPSULE_UI_PROOF={shlex.quote(judges)} PYTHONNOUSERSITE=1; {cmd}"
        done = exec_mcp._run_sandboxed(cfg, cmd, timeout)
        if done["exit_code"] == -1:
            return 124, f"timed out after {timeout}s"
        return done["exit_code"], done["text"]
    run.wt, run.lock, run.lane = wt, lock, None
    return run


def run_blind(lock, product, snap, out, line, timeout, results, stopped=None):
    """(exit code, output) of the blind suite's run `line` (the docker form: `/blind` is the
    snapshot) on the host, for a mac card (D305, B6), through `session`: the caller holds the
    lane lock (`lane`, passed as `lock`). The suite runs under the Mac profile on the neutral
    checkout `product`, READ-ONLY (D17): `BLIND_WORKSPACE` names it. The runner is Xcode's own
    python3; the snapshot `snap` is the only other readable path and the suite writes only inside
    an empty scratch box. Its `results` file is copied to `out` as a regular file of at most
    1 MiB + 1 byte. A timeout is exit 124; `stopped` set kills the run (exit -1)."""
    xcode = _probe()[1]
    if xcode is None:
        raise RuntimeError(NO_XCODE)
    if "/blind" not in line:
        raise ValueError("the blind run line has no /blind to point at the snapshot")
    python = shlex.quote(f"{xcode}/usr/bin/python3")    # the /usr/bin shim needs xcodebuild
    line = line.replace("/blind", shlex.quote(str(snap))).replace("exec python3 ",
                                                                   f"exec {python} ")
    with session(product, lock=lock, read=(snap,), copy=False) as run:
        if run.lane:
            raise RuntimeError(run.lane)
        box_out = Path(run.wt) / host.SCRATCH / "out"
        box_out.mkdir()
        done = threading.Event()
        if stopped is not None:
            threading.Thread(target=_kill_if_stopped, args=(stopped, done, run.wt),
                             daemon=True).start()
        try:
            work = shlex.quote(os.path.realpath(product))
            code, text = run(f'export BLIND_WORKSPACE={work} PYTHONDONTWRITEBYTECODE=1; '
                             f'{line} --out "$PWD/{host.SCRATCH}/out"', timeout)
        finally:
            done.set()
        host._kill_under(run.wt)        # no child left to swap a path between check and open
        if os.path.realpath(box_out) == str(box_out):   # a link on a parent dir: not read
            _copy_result(box_out / results, Path(out) / results)
    return (-1, text) if stopped is not None and stopped.is_set() else (code, text)


def _kill_if_stopped(stopped, done, wt):
    """Thread body: kills what runs under `wt` once `stopped` is set; ends when `done` is."""
    while not done.wait(1):
        if stopped.is_set():
            host._kill_under(wt)
            return


def _copy_result(src, dest):
    """The product's results file as a regular file at `dest`: no link followed, no FIFO read,
    at most 1 MiB + 1 byte copied (the reader treats anything over 1 MiB as unusable)."""
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return
    with os.fdopen(fd, "rb") as handle:
        if stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            dest.write_bytes(handle.read((1 << 20) + 1))
