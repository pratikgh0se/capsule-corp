"""Where a candidate's own code runs for the lander (decision row 632 b): inside the board's worker
image, never on the Mac (a mac card's checks are the one exception: `mac.checks` runs them
on the host under the Mac lane profile). The lander's suites (`contract_commands`), the mutation
gate that runs the candidate's tests, and the landing's after-push suite check all go through
`session`.

The container is the tick's own (`runners/docker.py`: cap-drop ALL, no-new-privileges,
--network=none, host uid, tmpfs, cpu/memory/pids limits), with a private COPY of the throwaway
clone at /workspace (the host never runs git on a tree the container could write: a planted
hook or config in that .git would otherwise run on the host) and only the trusted checkout's
`tests/` tree, extracted from its HEAD, read-only at /trusted. Nothing is inherited from the host
environment; there is no docker socket, so a suite that needs docker is never run here (the
lander holds that card). It carries no board label, so the tick's reaper (which removes labelled
containers of runs it does not know) never touches a landing in progress.

Unavailable (docker down, no image, a failed start, a container gone while the docker daemon is
down too) is `rc None`, never an exit code: every real exit code, 125 included, is the command's
own result. A container that is gone while the daemon is up is the command's own doing (a candidate
can kill PID 1): its exit code stands, so it cannot turn its own red into a wait.
"""
import contextlib
import io
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from capsule import paths
from capsule.runners import _blind_repo, docker

TRUSTED = "/trusted"       # the trusted checkout's tests/ tree, mounted read-only
WORKSPACE = "/workspace"   # the container's private copy of the clone
CPUS = "1"                 # spike 2026-10-29: the suite at 1 cpu is within 2x of the host
MARGIN = 600               # seconds a container outlives the commands it was sized for
ENV = ("AGENT_OPS_NO_SEND=1", "OBSIDIAN_VAULT_PATH=/tmp/vault", "AGENT_OPS_STATE_DIR=/tmp/state",
       "CAPSULE_OPS_PY=python3")
PREP = "mkdir -p /tmp/vault /tmp/state; "

NO_IMAGE = "needs a project image"    # leads the `why` of a sandbox that has no stack image


def _trusted_tests(dest):
    """The trusted checkout's `tests/` tree at its HEAD, extracted into `dest`."""
    done = subprocess.run(["git", "-C", str(paths.REPO), "archive", "HEAD", "tests"],
                          capture_output=True, timeout=120, check=True)
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        tar.extractall(dest, filter="data")


def _start(runner, src, tmp, seconds):
    """(container name, "") or (None, why): docker down, no worker image, or a failed start."""
    if not runner.available():
        return None, "docker is not available"
    run_id = secrets.token_hex(6)
    try:
        shutil.copytree(src, tmp / "w", symlinks=True)
        _trusted_tests(tmp / "t")
        name = runner.start(run_id, str(tmp / "w"), tmpdir=docker.SCRATCH_TMPDIR,
                            mounts=(f"{tmp / 't'}:{TRUSTED}:ro",), env=ENV, cpus=CPUS,
                            lifetime=seconds + MARGIN)
    except docker.MissingImage as exc:     # a hold, not a docker wait: the image must be built
        with contextlib.suppress(Exception):
            runner.stop(docker.container_name(run_id))
        return None, f"{NO_IMAGE}: {exc}"[:400]
    except (OSError, subprocess.SubprocessError, tarfile.TarError, docker.EngineDown) as exc:
        with contextlib.suppress(Exception):    # a start that failed half-way
            runner.stop(docker.container_name(run_id))
        return None, f"the worker container did not start: {exc} {getattr(exc, 'stderr', '')}"[:400]
    return name, ""


def _remove(runner, name, tmp):
    """Stop the container (--rm may have removed a dead one already), then delete its tmp dir."""
    try:
        if name is not None:
            with contextlib.suppress(RuntimeError, OSError, subprocess.SubprocessError):
                runner.stop(name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def runner_for(cfg, home=None):
    """The lander's strong runner, scoped like the tick's (`runners.scoped`): it carries the
    board's blind repo, spool root and (the lander's only) the tester's clones under `blind-work`,
    the places the mount audit looks for blind material in a container's binds, so a lander
    container is audited the way a worker's is. (The board is not a label: the tick's reaper must
    never touch a landing in progress.)"""
    runner = docker.StrongRunner(home=home)
    runner.blind_repo = _blind_repo(cfg)
    board_dir = paths.board_dir(cfg["board"]) if cfg.get("board") else None
    runner.image_board = cfg.get("board")    # its image row, never a label: the reaper skips it
    runner.spool_root = board_dir and board_dir / "spool"
    runner.blind_work = board_dir and board_dir / "blind-work"      # the tester's clones
    return runner


@contextlib.contextmanager
def session(src, cfg, seconds, runner=None, home=None):
    """Yield `run(cmd, timeout) -> (rc, output)`: one shell command in a container holding a copy
    of the clone `src` (None when there is no clone: yields None). `seconds` is what the caller's
    commands can take at most; the container stops on its own that long (plus MARGIN) after its
    start. When the sandbox cannot start, or its container is gone and docker is down after a
    failing command, `run` returns `(None, why)` and nothing runs anywhere else. A timeout is
    `(124, ...)`."""
    if src is None:
        yield None
        return
    runner = runner or runner_for(cfg, home)
    tmp, name, why = Path(tempfile.mkdtemp(prefix="lander-box-")), None, ""

    def run(cmd, timeout):
        if name is None:
            return None, why
        try:
            rc, out = runner.exec(name, PREP + cmd, timeout)
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s"
        if rc != 0 and not runner.alive(name) and not runner.available():
            return None, "the sandbox died mid-run: the container is gone and docker is down"
        return rc, out

    try:
        name, why = _start(runner, src, tmp, seconds)   # a mount-audit refusal raises: tmp goes too
        yield run
    finally:
        _remove(runner, name, tmp)
