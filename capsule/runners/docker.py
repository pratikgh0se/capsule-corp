"""Strong runner tier: a hardened, network-none Docker container per run (spec §6.6, rows 373, 395).

Security args are lifted from NousResearch/hermes-agent @ 98e87ac (MIT, Copyright (c) 2025
Nous Research; capsule/licenses/hermes-agent.LICENSE): tools/environments/docker.py:374-382
(cap-drop/add, no-new-privileges, /tmp and /var/tmp tmpfs), :415-416 (/run tmpfs), :976-977
(--network=none), :1319-1324 (--user <uid>:<gid>), :957-970 (--cpus/--memory/--pids-limit).
Differences on purpose (spec §6.6): no persist mode; label capsule-run-id=<run_id> plus
capsule=1; -v <workspace>:/workspace; the deps cache mount (DEPS_MOUNT below),
resolved from Path.home() at run time; -e TMPDIR=/workspace/.worker-exec (row 357); image from
the image record: the board's `capsule-proj-<board>` row, else the `capsule-python` row of
$CAPSULE_HOME/images.lock (OPS_WORKER_IMAGE wins), and no image, no fallback, when it holds none.
Container started with
`docker run -d --init ... <image> sleep infinity` (the upstream pattern).

The workspace mount carries no git dir (it lives beside the clone);
rows 440 and 445 mount it READ-ONLY at `container_git_dir` (where the clone's relative `.git`
pointer reaches; its host path comes up empty on Docker Desktop 4.43.1) into the contract and
implementer containers, and the run's spool `requests/` is mounted at `/requests` (the exec
tool's description names it; the host-side `claude` and the tick keep using the host path);
the spool root (run.json, report/) is never mounted. No bind mount may have its container
target equal to its host source: Docker Desktop 4.43.1 brings such a mount up empty
(`run_args` refuses to build one).
The implementer's `claude` runs on the host: acceptEdits confines Edit/Write to its cwd
(the workspace) and `--add-dir` (requests/), and `--allowedTools` pre-approves the exec
MCP tool, which `--tools` (built-ins only) cannot name.
"""
import contextlib
import fcntl
import json
import locale
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

from capsule import docker_door, engine_heal, jobs_dir, paths

# Timeouts (row 357: no docker call may block forever).
_QUICK_TIMEOUT = 30
_START_TIMEOUT = 300

# A container name we started always looks like this; `stop`/`reap` refuse anything else
# (name+label must both prove ownership before we touch a container).
_NAME_RE = re.compile(r"^capsule-[0-9A-Za-z]{1,12}$")

# Fixed hardening flags, independent of any one run (source: see module docstring).
SECURITY_ARGS = (
    "--cap-drop", "ALL",
    "--cap-add", "DAC_OVERRIDE",
    "--cap-add", "CHOWN",
    "--cap-add", "FOWNER",
    "--security-opt", "no-new-privileges",
    "--tmpfs", "/tmp:rw,exec,nosuid,size=512m",
    "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=256m",
    "--tmpfs", "/run:rw,noexec,nosuid,size=64m",
    "--network=none",
)

DEPS_MOUNT = ".local/share/capsule-corp/deps"

# Docker Desktop 4.43.1 sometimes starts a container whose bind mounts are missing (2026-10-31:
# 1-3 minute windows, every -v mount). start() probes the mounts and retries a fresh container;
# the sleeps (seconds, after try 1 and 2) keep the total under a minute.
_START_TRIES = 3
ENGINE_WAIT_S, ENGINE_POLL_S = 90, 3    # total budget (polls and probes) for a down engine
MEMO_FIRST_S, MEMO_CAP_S = 60, 900     # after a failed wake no new wake for this long, doubling
WOKE = {"failed_at": None, "delay": 0, "count": 0}   # the last failed wake (the tick's clock)
RESTARTS_PER_DAY, RESTART_GAP_S = 6, 3600   # hung-engine hard restarts: per rolling day; min gap
TICK_NOW = [None, 0.0]     # the tick sets [pass `now`, monotonic stamp]; None means `time.time()`


def _clock():
    if TICK_NOW[0] is None:
        return time.time()
    return TICK_NOW[0] + (time.monotonic() - TICK_NOW[1])


def restart_stamps(home=None, now=None):
    """The hard-restart stamps of the last 24 h: one host-wide file under CAPSULE_HOME, so a
    tick restart or a second board's tick cannot reset the count."""
    now = _clock() if now is None else now
    try:
        rows = json.loads((paths.home(home) / "engine-restarts.json").read_text())
    except (OSError, ValueError):
        return []
    return [t for t in rows if isinstance(t, (int, float)) and 0 <= now - t < 86400] \
        if isinstance(rows, list) else []


def note_failure():
    """Record one failed wake: the memo starts now, the delay doubles (60 s up to 900 s)."""
    WOKE.update(failed_at=_clock(), count=WOKE["count"] + 1,
                delay=min(WOKE["delay"] * 2 or MEMO_FIRST_S, MEMO_CAP_S))
_START_BACKOFF = (15, 30)
# The failure's command text: stable across runs, so the tick's outbox dedupe sees one message.
_PROBE_CMD = "mount-probe"
# A bounded, non-fatal listing of every directory under /workspace (never `.git`): a directory
# listing that stats each entry heals a stale Docker Desktop file-share entry (canary 2026-10-31),
# and a file the host rewrote in a nested dir is stale until its directory is listed (inc_7417f9a9).
# It ends with `; ` (never `&&`) and sends its output away, so it cannot fail or change what
# follows.
TREE_LISTING = ("timeout 10 find /workspace -name .git -prune -o -type d "
                "-exec ls -la {} + >/dev/null 2>&1; ")


def container_git_dir(workspace, git_dir):
    """Where a container that mounts `workspace` at /workspace sees the host `git_dir`: the
    path the clone's relative `.git` pointer (`../.gitdirs/<name>`) reaches from /workspace,
    e.g. `/.gitdirs/<name>`. Never the host path: Docker Desktop 4.43.1 mounts a source at
    its own path empty. Raises ValueError unless that is exactly `/.gitdirs/<name>`."""
    name = os.path.basename(git_dir)
    if os.path.relpath(git_dir, workspace) != f"../.gitdirs/{name}":
        raise ValueError(f"git dir {git_dir} is not at ../.gitdirs/<name> beside {workspace}")
    return f"/.gitdirs/{name}"


def _mount_probe(git_dir, requests=False):
    """The shell test that the workspace (and the git dir, and `/requests`, when mounted) are in
    the container; `git_dir` is its container path. The image bakes an empty /workspace, so only
    the clone's own repo proves the mount, and it is READ, not stat'ed: a stale Docker Desktop
    file-share entry passes `test -e` but fails `git rev-parse --verify HEAD` (2026-10-31). The
    read is preceded by `TREE_LISTING` (every directory under /workspace, bounded, never fatal)
    and a listing of /workspace (and the git dir): a directory listing that stats every entry
    heals a stale entry (canary, 2026-10-31), and a retry runs the heal again."""
    listed = " ".join(["/workspace", *([shlex.quote(str(git_dir))] if git_dir else [])])
    quoted = shlex.quote(str(git_dir)) if git_dir else ""
    tests = [f"ls -la {listed} >/dev/null", "git -C /workspace rev-parse --verify HEAD",
             # the `.git` pointer must reach the mounted git dir, not just any repo
             *([f'test "$(git -C /workspace rev-parse --absolute-git-dir)" = {quoted}']
               if git_dir else []),
             *([f"test -d {REQUESTS_MOUNT}"] if requests else []),
             *([f"test -d {quoted}"] if git_dir else [])]
    return TREE_LISTING + " && ".join(tests)


# R33 (blind spec 4.4 items 1-3): no container but the blind run's may mount the board's blind repo
# or a spool's blind / blind-out folder. The check name is the incident fingerprint's first half.
BLIND_CHECK = "implementer-has-no-blind"
BLIND_DIRS = ("blind", "blind-out")
BLIND_FOUND = 3
BLIND_TEST = f"test ! -e /blind || exit {BLIND_FOUND}"


def _norm(path):
    """`path` as the audit compares it: Docker Desktop's `/host_mnt` prefix dropped, symlinks
    resolved, case folded (the host file system is case-insensitive)."""
    text = str(path)
    if text == "/host_mnt" or text.startswith("/host_mnt/"):
        text = text[len("/host_mnt"):] or "/"
    return os.path.realpath(text).casefold()


def _within(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _blind_mount(source, blind_repo, spool_root=None, blind_work=None):
    """True when the bind source is, holds or sits under `blind_repo` or `blind_work` (the root of
    the tester's clones, `<board_dir>/blind-work`); or holds the spool root, is one of its run
    folders or sits under a run's blind / blind-out folder; or, whatever the spool root, sits
    under a `spool/<run_id>/blind` path."""
    real = _norm(source)
    for root in (blind_repo, blind_work):
        root = root and _norm(root)
        if root and (_within(real, root) or _within(root, real)):
            return True
    root = spool_root and _norm(spool_root)
    if root and (_within(root, real) or (_within(real, root) and (
            len(Path(real[len(root):]).parts) == 2 or any(
                p in BLIND_DIRS for p in Path(real[len(root):]).parts[2:3])))):
        return True
    parts = Path(real).parts
    return any(p in BLIND_DIRS and i >= 2 and parts[i - 2] == "spool" for i, p in enumerate(parts))


def _mount_sources(text):
    """The bind sources of a `docker inspect` document (one container's list, or a bare list of
    mounts), or None unless it is exactly that with at least one non-tmpfs mount, each with a
    Source: an empty or partial answer is never a clean one."""
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if isinstance(doc, list) and len(doc) == 1 and isinstance(doc[0], dict) and "Mounts" in doc[0]:
        doc = doc[0]["Mounts"]
    if isinstance(doc, list):    # a tmpfs has no host source (the list may or may not show it)
        doc = [m for m in doc if not (isinstance(m, dict) and m.get("Type") == "tmpfs")]
    if not isinstance(doc, list) or not doc or not all(
            isinstance(m, dict) and isinstance(m.get("Source"), str) and m["Source"] for m in doc):
        return None
    return [m["Source"] for m in doc]


def container_name(run_id):
    return f"capsule-{run_id[:12]}"


def _ops_env_image():
    """`OPS_WORKER_IMAGE` from the operator's ops.env (`OPS_ENV_FILE` overrides the path), the
    same pin `maintenance/board_bridge.py` reads; only that one key is ever parsed (pilot prep)."""
    path = os.environ.get("OPS_ENV_FILE") or str(jobs_dir.config_dir() / "ops.env")
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    found = ""
    for raw in lines:   # the last assignment wins, as `deploy.sh` (`tail -1`) and a shell `source` read it
        key, sep, value = raw.strip().partition("=")
        if sep and key.strip() == "OPS_WORKER_IMAGE":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            found = value
    return found


# The one lock file. Never taken from the environment: the image files are protected paths (D39,
# D7), so nothing a worker runs with may redirect which image the next run starts on.
LOCK_FILE = paths.REPO / "docker" / "IMAGES.lock"


BUILD_FLOOR_FILE = paths.REPO / "docker" / "BUILD_MIN_FREE_BYTES"   # build-base.sh reads it too


def build_floor():
    """The free disk (bytes) build-base.sh refuses under, from `BUILD_FLOOR_FILE`; None when
    the file is missing or does not hold 1 to 15 digits, no leading zero, once spaces and
    newlines are trimmed from its ends (the rule build-base.sh applies; a CR, sign or
    underscore is refused, so the bytes are read raw, not through universal newlines)."""
    try:
        text = BUILD_FLOOR_FILE.read_bytes().decode("latin-1").strip(" \n")
    except OSError:
        return None
    return int(text) if re.fullmatch(r"[1-9][0-9]{0,14}", text) else None


_FLOOR = build_floor()
BUILD_COMMAND = "docker/build-base.sh base && docker/build-base.sh python"
BUILD_NOTE = (f"needs {_FLOOR // 10**9} GB free and takes hours" if _FLOOR
              else "needs a lot of free disk and takes hours")


def build_command(home=None):
    """What a tester runs from their clone to build the worker image: `BUILD_COMMAND`, led by an
    `export CAPSULE_HOME=...` when `home` is not the default, so the record lands where the tick
    reads it."""
    if home is None or Path(home) == paths.default_home():
        return BUILD_COMMAND
    return f"export CAPSULE_HOME={shlex.quote(str(home))} && {BUILD_COMMAND}"


def lock_image(lock=None, name="capsule-python"):
    """The `name` row (default `capsule-python`) of docker/IMAGES.lock (or of `lock`), or "".
    `worker_image` calls it. A `name` row that is not
    `name@sha256:<64 hex>` (a bare tag) is refused with one stderr warning."""
    try:
        lines = (lock or LOCK_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):   # missing, unreadable or not text
        return ""
    rows = [ln.strip() for ln in lines if ln.startswith((f"{name}@", f"{name}:"))]
    if rows and not re.fullmatch(rf"{re.escape(name)}@sha256:[0-9a-f]{{64}}", rows[-1]):
        print(f"docker runner: lock row {rows[-1]!r} is not a digest, refused", file=sys.stderr)
        return ""
    return rows[-1] if rows else ""


def record_image(home=None):
    """The `capsule-python` row of $CAPSULE_HOME/images.lock, the image `docker/build-base.sh`
    built on this machine, or "" (no file, no row, or a row that is not a digest: no image). It
    never falls back to the repo's lock, whose digests are the maintainer's."""
    return lock_image(paths.image_record(home))


def project_image_name(board):
    """The repository name of a board's project image: docker names are lowercase."""
    return f"capsule-proj-{board.lower()}"


def image_override():
    """Any operator pin, `OPS_WORKER_IMAGE` in the environment, else in ops.env, or "" (is a pin
    set at all; `worker_image_source` says which source wins)."""
    return os.environ.get("OPS_WORKER_IMAGE") or _ops_env_image()


def worker_image_source(home=None, board=None):
    """(image, source) of the worker image, the more specific winning (D328): `OPS_WORKER_IMAGE`
    in the process environment (a per-run override), then the board's project row
    (`project_image_name(board)`) in $CAPSULE_HOME (`home`), then `OPS_WORKER_IMAGE` in ops.env
    (the machine-wide default), then the `capsule-python` row. Nothing: ("", "none")."""
    project = board and lock_image(paths.image_record(home), project_image_name(board))
    for image, source in ((os.environ.get("OPS_WORKER_IMAGE"), "environment OPS_WORKER_IMAGE"),
                          (project, "project row"),
                          (_ops_env_image(), "ops.env OPS_WORKER_IMAGE"),
                          (record_image(home), "capsule-python row")):
        if image:
            return image, source
    return "", "none"


def worker_image(home=None, board=None):
    """The image of `worker_image_source`, or "" (no image, no fallback to any other)."""
    return worker_image_source(home, board)[0]


def missing_image_message(board, stacks=()):
    """What `image which` and the runner say when no image is usable for `board`; with the
    `stacks` its workspace declares, it names them."""
    need = (f" (its capsule.toml declares stacks {list(stacks)}; the generic image lacks them)"
            if stacks else "")
    return (f"no image recorded for board {board!r}{need}: build one with "
            f"`python3 -m capsule image build --project DIR --board {board}`")


class MissingImage(RuntimeError):
    """A start refused for want of the project image its workspace declares. Not a crash: the
    tick holds the card (`incident_blind.hold`, kind capability) until the image is built."""


def asks_generic(stacks):
    """True when a declared stacks list is exactly the base python stack (the generic image's)."""
    return stacks == ["python"]


def _declared_stacks(workspace):
    """The non-empty `[image].stacks` list of `<workspace>/capsule.toml`, else []: no file, no
    table, an empty or non-list value, or a file `tomllib` cannot read all mean "none declared"."""
    try:
        with open(Path(workspace) / "capsule.toml", "rb") as fh:
            stacks = tomllib.load(fh).get("image", {}).get("stacks")
    except (OSError, ValueError, AttributeError):
        return []
    return stacks if isinstance(stacks, list) and stacks else []


# The contract (validation) and implementer containers' scratch (row 445): the /tmp tmpfs
# (rw,exec,nosuid; Docker's tmpfs default is noexec, so `exec` is explicit) instead
# of /workspace/.worker-exec. On Docker Desktop the workspace bind mount does not keep POSIX
# semantics a suite relies on (a 0500 dir stays writable to its owner; os.replace is not atomic to
# a concurrent reader), so a green suite failed in the image at baseline (row 440).
CONTRACT_TMPDIR = "/tmp"
# The lander's scratch (decision row 632 b): a second exec tmpfs, so a suite's temp dirs are not
# under /tmp (a test that greps /tmp must not walk into its own sandbox's fake secrets).
SCRATCH_TMPDIR = "/scratch"
REQUESTS_MOUNT = "/requests"   # the run's spool requests/ (never at its host path, see run_args)


def run_args(run_id, workspace, image, *, cpus="1", memory_mb=5120, requests_dir=None, git_dir=None,
             tmpdir=None, board=None, ro=False, mounts=(), env=(), lifetime=None):
    """The full `docker run` argv for one run's container (lifted security args + Capsule mounts).

    `git_dir` (the contract and implementer containers): the clone's host git dir, mounted
    READ-ONLY at `container_git_dir` (never its host path) so the workspace's relative `.git`
    pointer resolves and a suite's `git ls-files` works; read-only keeps that intact: nothing
    in the container can write the config or hooks the host's git reads.
    `tmpdir` overrides TMPDIR (CONTRACT_TMPDIR for both).
    `ro` (the blind run, R28) mounts the workspace read-only; `mounts` are extra `src:dst[:ro]`
    volume specs. Neither touches the security args or the network.
    `env` (`K=V` strings) are extra container variables; `lifetime` (seconds) replaces
    `sleep infinity` and adds `--rm`, so a container whose owner was killed stops and goes."""
    if not hasattr(os, "getuid"):
        # The upstream skips --user entirely here (1319-1326); we have no non-root fallback to
        # offer, and an implicit root default is the wrong direction for a security tier.
        raise RuntimeError("run_args needs POSIX getuid/getgid; no implicit root fallback")
    uid, gid = os.getuid(), os.getgid()
    deps = Path.home() / DEPS_MOUNT
    volumes = [f"{workspace}:/workspace{':ro' if ro else ''}", f"{deps}:/deps:ro", *mounts,
               *([f"{requests_dir}:{REQUESTS_MOUNT}"] if requests_dir else []),
               *([f"{git_dir}:{container_git_dir(workspace, git_dir)}:ro"] if git_dir else [])]
    for spec in volumes:
        source, target = spec.split(":")[:2]
        if source == target:
            raise ValueError(f"bind mount {spec!r} has its container target equal to its host "
                             "source: Docker Desktop 4.43.1 brings that up empty")
    return [
        "docker", "run", "-d", "--init", *(("--rm",) if lifetime else ()),
        *SECURITY_ARGS,
        *(("--tmpfs", f"{SCRATCH_TMPDIR}:rw,exec,nosuid,size=512m") if tmpdir == SCRATCH_TMPDIR
          else ()),
        "--cpus", str(cpus),
        "--memory", f"{memory_mb}m",
        "--pids-limit", "256",
        "--user", f"{uid}:{gid}",
        "--label", "capsule=1", "--label", "capsule.kind=run",
        "--label", f"capsule-run-id={run_id}",
        *(("--label", f"capsule-board={board}") if board else ()),
        *(arg for spec in volumes for arg in ("-v", spec)),
        "-e", f"TMPDIR={tmpdir or '/workspace/.worker-exec'}",
        # the uid has no passwd entry and `/` is read-only: Path.home() needs a writable dir
        "-e", "HOME=/tmp",
        *(arg for item in env for arg in ("-e", item)),
        "--name", container_name(run_id),
        image, "sleep", str(lifetime or "infinity"),
    ]


CUT_MIDDLE = "\n[output middle cut: first and last bytes kept]\n"
CUT_OPEN = "\n[output cut: stream still open after exit]\n"
_READER_GRACE = _QUICK_TIMEOUT   # seconds the readers get after the command exits


def _drain(pipe, cap, kept):
    """Read `pipe` to its end, keeping at most `cap` + 1 bytes (one past the cap, so a cut is
    seen) as the head and the tail of the stream, so a traceback at the end survives; the
    middle is read and dropped, so the writer never blocks. A cut puts `CUT_MIDDLE` between
    head and tail in `kept` (one buffer), where it happened."""
    head_n = (cap + 2) // 2
    tail_n = cap + 1 - head_n
    head, tail, total = bytearray(), bytearray(), 0
    for chunk in iter(lambda: pipe.read1(65536), b""):
        total += len(chunk)
        room = head_n - len(head)
        head += chunk[:room]
        tail += chunk[room:] if room > 0 else chunk
        del tail[:max(len(tail) - tail_n, 0)]
        mark = CUT_MIDDLE.encode() if total > cap + 1 else b""
        kept[:] = [bytes(head) + mark + bytes(tail)]
    pipe.close()


def _capped_run(argv, *, cap, timeout, env=None, **_):
    """`subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout)`
    that keeps at most `cap` + 1 bytes of each stream (head and tail, `CUT_MIDDLE` added when
    cut): a CompletedProcess with the stdout and stderr as read. A timeout kills the command
    and raises `subprocess.TimeoutExpired`."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    kept = ([], [])
    readers = [threading.Thread(target=_drain, args=(pipe, cap, into), daemon=True)
               for pipe, into in zip((proc.stdout, proc.stderr), kept)]
    for reader in readers:
        reader.start()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        end = time.monotonic() + _READER_GRACE     # one budget for both; the threads are daemons
        for reader in readers:
            reader.join(max(end - time.monotonic(), 0))
    out, err = (_text(part, reader.is_alive()) for part, reader in zip(kept, readers))
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _text(parts, open_):
    """What `subprocess.run(text=True, errors="replace")` makes of the bytes: the locale's
    encoding and universal newlines. A stream a leftover process still held open when the
    reader's grace ran out says so, so a cut is never read as the whole output."""
    text = b"".join(parts).decode(locale.getpreferredencoding(False), "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text + CUT_OPEN if open_ else text


class EngineDown(RuntimeError):
    """The Docker engine app did not answer after `_ensure_engine` opened it (not a crash)."""


def recently_down(now=None):
    """True while the memo of the last failed wake (60 s, doubling to 900 s) runs."""
    at = WOKE["failed_at"]
    return at is not None and (_clock() if now is None else now) - at < WOKE["delay"]


class StrongRunner:
    """Docker-backed runner: one hardened, network-none container per run."""

    home = None   # whose image record the runner reads; None is `paths.home()`
    cap = None    # bytes of an `exec`'s output kept per stream, plus one; None keeps all
    git_identity = None   # the board's `{name, email}` (`scoped`); None keeps the image's worker
    image_board = None    # the board whose project image row a no-label runner (the lander's) uses

    def __init__(self, docker="docker", board=None, home=None):
        self.docker = docker
        self.home = home
        self.board = board   # the tick's board: start() labels with it, reap() touches only it
        self.blind_repo = None   # the board's blind repo path (`scoped`): no container mounts it
        self.spool_root = None   # the board's spool root (`scoped`): nor a run folder under it
        self.blind_work = None   # the tester's clones (`scoped`): nor one of them
        self._run = subprocess.run
        self._run_capped = _capped_run  # the `_run` of an `exec` with a `cap`; injectable too
        self._sleep = time.sleep   # injectable, like `_run`
        self._open = subprocess.run  # starts the engine app; injectable, like `_run`
        self._hung, self._err = False, ""   # the last `docker info`: timed out, or its error tail

    def _d(self, *args, **kw):
        """Every docker call of this runner: the guarded door, with this runner's `_run`."""
        return docker_door.docker_run(list(args), self._run, docker=self.docker, **kw)

    def available(self):
        if os.environ.get("AGENT_OPS_NO_DOCKER"):
            return False
        if not worker_image(self.home, self.board or self.image_board):
            return False    # nothing built yet: the tier is down, cards wait
        try:
            r = self._d("version", "--format", "{{.Server.Version}}",
                    capture_output=True, text=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    def _engine_up(self):
        """True when `docker info` answers. A False sets `_hung` (the probe timed out, not refused)
        and `_err` (why), for `_ensure_engine`."""
        self._hung, self._err = False, ""
        try:
            r = self._d("info", capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self._hung, self._err = True, "docker info timed out"
            return False
        except OSError as exc:
            self._err = str(exc)
            return False
        self._err = "" if r.returncode == 0 else str(getattr(r, "stderr", "") or "")[-120:].strip()
        return r.returncode == 0

    def _poll_engine(self):
        """Poll `docker info` up to ENGINE_WAIT_S in total; True (memo cleared) once it answers.
        On False, `_hung` says whether EVERY probe timed out (a hung engine, not a slow one)."""
        spent, hung = 0, True
        while spent < ENGINE_WAIT_S:
            self._sleep(ENGINE_POLL_S)
            t0 = time.monotonic()
            up = self._engine_up()
            hung = hung and self._hung
            spent += ENGINE_POLL_S + (time.monotonic() - t0)
            if up:
                WOKE.update(failed_at=None, delay=0, count=0)
                return True
        self._hung = hung
        return False

    def _hard_restart(self):
        """A hung OrbStack (every probe of the wait timed out): `orb stop`, `orb start`, then poll
        again, all under a host-wide lock, so two ticks never restart together (the one that
        cannot get it waits for the poll result). At most RESTARTS_PER_DAY a day, one per
        RESTART_GAP_S, none while the keeper lands. A skip prints why. True when docker answers."""
        row, now, root = docker_door.docker_engine_row(), _clock(), paths.home(self.home)
        root.mkdir(parents=True, exist_ok=True)
        with open(root / "engine-restart.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return self._poll_engine()
            stamps = restart_stamps(self.home, now)
            queue = paths.queue_dir("", self.home)
            if engine_heal.skip_reason(
                    row.get("name"), stamps, now, RESTARTS_PER_DAY, RESTART_GAP_S,
                    any(paths.keeper_claims(sorted(queue.glob("*/owners.json")), queue, now)),
                    (root, row.get("bin"))):
                return False
            (root / "engine-restarts.json").write_text(json.dumps([*stamps, now]))
            print(f"capsule: docker engine hung, hard restart {len(stamps) + 1} of "
                  f"{RESTARTS_PER_DAY} today", file=sys.stderr)
            try:
                for verb in ("stop", "start"):
                    self._open([f"{row['bin']}/orb", verb], capture_output=True, text=True,
                               timeout=60)
            except (OSError, subprocess.SubprocessError):
                return False
            return self._poll_engine()

    def _ensure_engine(self):
        """False when `docker info` answers, or when the engine cannot be opened here (not macOS,
        or `open` fails: the caller re-raises its own docker error). Else (the idle reaper quit
        the engine) `open -a <engine app>`, poll within 90 s in total and return True, or raise
        EngineDown. A probe that timed out in that wait (a hung OrbStack) gets a hard restart
        (`_hard_restart`). After a failed wake it raises at once (60 s, doubling to 900 s), so a
        pass wakes the engine once. The EngineDown text names the `open` exit and the last probe
        error."""
        if self._engine_up():       # probe first: an engine started by hand is used at once
            WOKE.update(failed_at=None, delay=0, count=0)
            return False
        if recently_down():         # the memo gates only the `open -a` start
            raise EngineDown("the Docker engine is still down")
        if sys.platform != "darwin":
            return False
        if "unittest" in sys.modules and self._open is subprocess.run:
            raise RuntimeError("a test would open the real engine: inject `_open`")
        app = docker_door.docker_engine_row()["app"]
        try:
            r = self._open(["open", "-a", app], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return False
        if self._poll_engine() or (self._hung and self._hard_restart()):
            return True
        note_failure()
        err = str(getattr(r, "stderr", "") or "")[-100:].strip()
        if self._hung and docker_door.docker_engine_row().get("name") == "orbstack":
            raise EngineDown(engine_heal.HUNG)
        raise EngineDown(f"the Docker engine did not start in 90 s (open -a {app}: exit "
                         f"{getattr(r, 'returncode', '?')}: {err}; docker info: {self._err})"[:300])

    def start(self, run_id, workspace, requests_dir=None, git_dir=None, tmpdir=None, *, ro=False,
              mounts=(), allow_blind=False, **more):
        """Start the run's container and prove it is sane: the bind mounts present (retried), and no
        blind mount (`allow_blind`, the blind run's own snapshot, output folder, clone and git dir
        paths, are the only exemption; else RuntimeError `<check>: <path>`, container removed)."""
        board = self.board or self.image_board    # the lander has no label, but a board's image
        image = worker_image(self.home, board)
        if not image:
            raise RuntimeError(missing_image_message(board))
        stacks = _declared_stacks(workspace)
        if (stacks and not asks_generic(stacks) and not os.environ.get("OPS_WORKER_IMAGE") and not
                lock_image(paths.image_record(self.home), project_image_name(board or ""))):
            # A declared stack needs its project image: the generic one lacks the stack's tools.
            raise MissingImage(missing_image_message(board, stacks))
        who = self.git_identity or {}
        env = [f"GIT_{role}_{key}={who[field]}" for role in ("AUTHOR", "COMMITTER")
               for key, field in (("NAME", "name"), ("EMAIL", "email")) if field in who]
        more["env"] = [*env, *more.get("env", ())]
        args = run_args(run_id, workspace, image, requests_dir=requests_dir,
                        git_dir=git_dir, tmpdir=tmpdir, board=self.board, ro=ro, mounts=mounts,
                        **more)
        name = container_name(run_id)
        for attempt in range(_START_TRIES):
            try:
                self._d(*args[1:], check=True, capture_output=True, text=True,
                        timeout=_START_TIMEOUT)
                rc, out = self._verify(name, git_dir and container_git_dir(workspace, git_dir),
                                       bool(requests_dir), allow_blind)
            except subprocess.CalledProcessError:
                if attempt == 0 and self._ensure_engine():
                    continue         # the engine was down: it is up now, start again
                self._engine_gone()
                raise                # docker refused the run: no container, nothing to clean
            except BaseException as exc:   # a run that hung, or any error: nothing may linger
                self._discard(name)
                if isinstance(exc, subprocess.TimeoutExpired):
                    self._engine_gone()
                    raise EngineDown("container start timed out") from None
                raise
            if rc == 0:
                return name
            if not self._discard(name):
                out += " (the broken container could not be removed)"
                break          # never start a new attempt into a name conflict; reap cleans later
            if attempt < len(_START_BACKOFF):
                self._sleep(_START_BACKOFF[attempt])
        raise subprocess.CalledProcessError(
            rc, _PROBE_CMD, out, f"bind mounts missing in a fresh container: {out.strip()}"[:400])

    def _engine_gone(self):
        """Raise EngineDown when `docker info` no longer answers (a start that failed or hung
        because the engine is down, not a crash); else return. Notes the failed wake only then."""
        if not self._engine_up():
            note_failure()
            raise EngineDown("the Docker engine does not answer")

    def _verify(self, name, git_dir, requests, allow_blind):
        """(rc, output): the mount probe, then (never skipped, whatever the probe said) the audit
        of the container's mounts, then for an implementer (`requests`) that /blind is not in it.
        A blind mount, or a /blind, raises. `allow_blind` (the blind run's own container) is the
        host paths of its own snapshot, output folder, clone and git dir: those binds are
        exempt, nothing else is, so the audit always runs."""
        rc, out = self._probe(name, git_dir, requests)
        audited = self._audit(name, allow_blind)
        if rc == 0 and audited[0] == 0 and requests:
            audited = self._no_blind(name)
        return (rc, out) if rc else audited

    def _audit(self, name, own=()):
        """(0, "") when no bind source of the container is blind material (but the `own` paths);
        raises RuntimeError when one is; (1, why) when the mount list cannot be read (a failed
        probe, never clean)."""
        try:
            r = self._d("inspect", name, capture_output=True, text=True, timeout=_QUICK_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, f"{type(exc).__name__}: {exc}"
        sources = _mount_sources(r.stdout or "") if r.returncode == 0 else None
        if sources is None:
            return 1, f"docker inspect mounts unreadable (rc={r.returncode})"
        exempt = {_norm(p) for p in own or ()}
        for source in sources:
            if _norm(source) not in exempt and _blind_mount(
                    source, self.blind_repo, self.spool_root, self.blind_work):
                raise RuntimeError(f"{BLIND_CHECK}: {source} [run {name}]")
        return 0, ""

    def _no_blind(self, name):
        """The second oracle (the filesystem's view): (0, "") when /blind is not there; raises
        RuntimeError when it is (exit 3, a code only the test's own `exit` gives: `docker exec`
        itself exits 1 on a daemon or no-such-container error); else (rc, why), a failed probe."""
        try:
            rc, out = self._exec(name, BLIND_TEST, _QUICK_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, f"{type(exc).__name__}: {exc}"
        if rc == BLIND_FOUND:
            raise RuntimeError(f"{BLIND_CHECK}: /blind [run {name}]")
        return rc, out

    def _probe(self, name, git_dir, requests):
        """(rc, output) of the mount probe; a probe that hangs or cannot run is a failed probe."""
        try:
            return self._exec(name, _mount_probe(git_dir, requests), _QUICK_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, f"{type(exc).__name__}: {exc}"

    def _discard(self, name):
        """`docker stop` then `docker rm` (never forced) of a container we started; True when no
        container of that name is left."""
        for verb in (("stop", "-t", "2"), ("rm",)):
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                self._d(*verb, name, capture_output=True, text=True, timeout=_QUICK_TIMEOUT)
        try:   # `--rm` containers vanish on stop, so a failed rm alone proves nothing
            return self._d("inspect", name, capture_output=True, text=True,
                           timeout=_QUICK_TIMEOUT).returncode != 0
        except (OSError, subprocess.SubprocessError):
            return False

    def exec(self, name, cmd, timeout):
        """`cmd` in the container, after TREE_LISTING in the same exec: a file the host replaced
        since the last listing is stale in the container until its directory is listed
        (inc_35435ea0). The exit code and output are the command's. The mount probe has its
        own listing and goes through `_exec`."""
        return self._exec(name, TREE_LISTING + cmd, timeout, self.cap)

    def _exec(self, name, cmd, timeout, cap=None):
        args = ("exec", "-w", "/workspace", name, "sh", "-lc", cmd)
        if cap:            # opted in: read as it comes, kept only up to `cap` (+1) per stream
            r = docker_door.docker_run(list(args), self._run_capped, docker=self.docker,
                                       cap=cap, timeout=timeout)
        else:
            r = self._d(*args, capture_output=True, text=True, errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def alive(self, name):
        """True only when docker says the container is running; a failed inspect is not alive."""
        try:
            r = self._d("inspect", "-f", "{{.State.Running}}", name, capture_output=True,
                        text=True, timeout=_QUICK_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0 and (r.stdout or "").strip() == "true"

    def stop(self, name):
        """Remove one container we started -- refuses anything not shaped like our own name."""
        if not _NAME_RE.match(name):
            raise ValueError(f"refusing to touch non-capsule container name: {name!r}")
        r = self._d("rm", "-f", name, capture_output=True, text=True, timeout=_QUICK_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(f"docker rm -f {name} failed (rc={r.returncode}): {(r.stderr or '').strip()}")

    def reap(self, live_run_ids):
        """Remove only capsule=1-labelled containers whose name AND run-id label both prove
        they are ours and whose run is not live (row 357 lesson: a container with an empty
        or mismatched run-id label, or a name we didn't mint, is never touched). Only containers
        labelled with THIS runner's board are considered: another board's, an unlabelled
        (legacy) one, or any container when no board is set is left alone."""
        ps = self._d("ps", "-a", "--filter", "label=capsule=1",
                     "--format", '{{.Names}}\t{{.Label "capsule-run-id"}}'
                                 '\t{{.Label "capsule-board"}}',
                     capture_output=True, text=True, timeout=_QUICK_TIMEOUT)
        if ps.returncode != 0:
            raise RuntimeError(f"docker ps failed (rc={ps.returncode}): {(ps.stderr or '').strip()}")
        removed = []
        for line in (ps.stdout or "").splitlines():
            if not line.strip():
                continue
            name, _, rest = line.partition("\t")
            run_id, _, board = rest.partition("\t")
            name, run_id, board = name.strip(), run_id.strip(), board.strip()
            if not self.board or board != self.board:
                continue  # another board's container, or one we cannot attribute
            if not run_id:
                continue  # never touch a container with no run-id label
            if name != container_name(run_id):
                continue  # name doesn't prove ownership of this run_id
            if run_id in live_run_ids:
                continue
            rm = self._d("rm", "-f", name, capture_output=True, text=True, timeout=_QUICK_TIMEOUT)
            if rm.returncode == 0:
                removed.append(name)
        return removed

    def limits(self, name, spool):
        spool = Path(spool).absolute()
        spool.mkdir(parents=True, exist_ok=True)
        mcp_path = spool / "mcp.json"
        cfg = {
            "mcpServers": {
                "capsule": {
                    "command": sys.executable,
                    # -P (3.11+ safe-path): this host process inherits claude's cwd, the worker's
                    # worktree; without it `-m` puts that cwd ahead of PYTHONPATH and a planted
                    # capsule/runners/exec_mcp.py would run unsandboxed on the host.
                    "args": ["-P", "-m", "capsule.runners.exec_mcp", "--container", name, "--spool",
                             str(spool), "--docker", self.docker],
                    "env": {"PYTHONPATH": str(paths.REPO)},
                }
            }
        }
        mcp_path.write_text(json.dumps(cfg))
        return {"tools": "Read,Edit,Write,Glob,Grep", "allowed": "mcp__capsule__exec",
                "permission_mode": "acceptEdits", "add_dirs": [str(spool / "requests")],
                "mcp_config": str(mcp_path)}
