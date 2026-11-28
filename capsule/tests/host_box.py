"""Tests only: the lander's sandbox (runners/sandbox.py) replaced by a shell in the host clone, so
the lander and landing tests keep their real-git, real-shell fixtures without a docker daemon. The
real `session` stays reachable as `sandbox.REAL_SESSION`; the tests of the sandbox itself
(test_sandbox.py) and of what the lander hands it use that, over a fake docker CLI."""
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from capsule import paths
from capsule.blind.checkout import SCRATCH
from capsule.runners import sandbox


@contextlib.contextmanager
def session(src, cfg, seconds=0, runner=None, home=None):
    """Stands in for `sandbox.session`: the container's view (/workspace, /trusted) is mapped to
    the clone and the trusted checkout, and the old pinned env is kept for the fixtures. It runs
    candidate code on the host, so it refuses to exist outside a test process."""
    if "unittest" not in sys.modules:
        raise RuntimeError("host_box.session runs commands on the host: tests only")
    if src is None:
        yield None
        return
    tmp = Path(tempfile.mkdtemp(prefix="hostbox-"))
    for name in ("vault", "home", "state", "t"):
        (tmp / name).mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("RUN_DOCKER_TESTS",
                                                              "CAPSULE_DOCKER_AUTO_BASE")}
    env.update(OBSIDIAN_VAULT_PATH=str(tmp / "vault"), AGENT_OPS_NO_SEND="1",
               CAPSULE_OPS_PY="python3",
               TMPDIR=str(tmp / "t"), HOME=str(tmp / "home"),
               AGENT_OPS_STATE_DIR=str(tmp / "state"))

    def run(cmd, timeout):
        # the worker scratch dir (the image's TMPDIR) is not in the clone: leave it alone
        mine = (re.escape(sandbox.WORKSPACE)
                + rf"(?=/(?!{re.escape(SCRATCH)}(?![\w.-]))|(?![\w./-]))")
        cmd = re.sub(mine, lambda _: str(src), cmd).replace(sandbox.TRUSTED, str(paths.REPO))
        try:
            done = subprocess.run(cmd, shell=True, cwd=str(src), text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s"
        return done.returncode, done.stdout

    try:
        yield run
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def docker_env(tmp):
    """Environment variables that put the fake `docker` (fake_docker.sh) first on a child's PATH,
    for tests that spawn the real landing worker: its sandbox then runs on the host shell."""
    bin_dir, state = Path(tmp) / "fakebin", Path(tmp) / "fakestate"
    bin_dir.mkdir()
    state.mkdir()
    script = Path(__file__).with_name("fake_docker.sh")
    (bin_dir / "docker").write_text(script.read_text())
    (bin_dir / "docker").chmod(0o755)
    return {"AGENT_OPS_DOCKER_BIN": str(bin_dir), "FAKE_DOCKER_STATE": str(state),
            "FAKE_DOCKER_TRUSTED": str(paths.REPO), "OPS_WORKER_IMAGE": "img:test"}
