"""obs_image_lock.py - does the worker image match the capsule-python row of docker/IMAGES.lock?
(t_53c8a81a). obs_probe prints the one-line answer each tick, so a drifted image shows up before
anyone starts a card on it. The image checked is the one the boards would run
(`docker.worker_image`). Read only: the lock is never
written, docker is asked only `image inspect`. Anything that stops the comparison (no docker,
daemon down, no python row, unreadable lock) is `n/a`, never a failure."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events         # noqa: E402  docker_run(): the same door every other collector uses
from capsule.runners import docker   # noqa: E402  worker_image(): the image a board would run

LOCK = Path(__file__).resolve().parents[1] / "docker" / "IMAGES.lock"
_ROW = re.compile(r"^capsule-python@(sha256:[0-9a-f]{64})$")
_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def _image():
    """The image a board would run: docker.worker_image(), so the OPS_WORKER_IMAGE and ops.env
    overrides apply exactly as `StrongRunner.start` applies them."""
    return docker.worker_image()


def _lock_digest(lock):
    try:   # `capsule-python@` / `capsule-python:` exactly as docker.lock_image matches a row
        rows = [ln.strip() for ln in Path(lock).read_text(encoding="utf-8").splitlines()
                if ln.startswith(("capsule-python@", "capsule-python:"))]
    except (OSError, ValueError):   # missing, unreadable or not text
        return None
    m = _ROW.match(rows[-1]) if rows else None
    return m.group(1) if m else None


def check(run, lock=None, image=None, home=None):
    """(state, line): state is `ok`, `mismatch` or `n/a`; line is one printable line. `home` is
    unused, kept for callers. The default lock is the record $CAPSULE_HOME/images.lock (what
    was built here), never the tracked lock."""
    want = _lock_digest(lock or docker.paths.image_record())
    if not want:
        return "n/a", "worker image n/a: no capsule-python digest row in the lock"
    try:
        image = image or _image()
    except OSError:   # the record is unreadable
        return "n/a", "worker image n/a: no worker image to inspect"
    try:
        r = events.docker_run(["image", "inspect", "--format", "{{.Id}}", image], run,
                              capture_output=True, text=True, timeout=30)
    except Exception as exc:   # no docker binary, a timeout, a stub that refuses: not a failure
        return "n/a", f"worker image n/a: docker unavailable ({type(exc).__name__})"
    got = (r.stdout or "").strip()
    if r.returncode != 0 or not _ID.match(got):
        return "n/a", "worker image n/a: docker could not inspect the image"
    if got == want:
        return "ok", f"worker image ok: {got[7:19]}"
    return "mismatch", f"worker image mismatch: image {got[7:19]} lock {want[7:19]}"
