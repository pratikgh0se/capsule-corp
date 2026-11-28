"""Per-run start-state record (EVAL-03 run recording): what a golden-set replay needs to rebuild
the code state a run began from. Written next to run.json as start_state.json, never fatal."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from capsule import safe_write, workspace

NAME = "start_state.json"
DIFF_NAME = "start_diff.patch"
DIFF_CAP = 1024 * 1024      # bytes of the uncommitted diff kept on disk


def job_image(runner, started):
    """The job.json entry naming the image a strong runner's container really runs: only when the
    runner started a container (`started`)."""
    from capsule.runners import docker
    if started and isinstance(runner, docker.StrongRunner):
        return {"runner_image": docker.worker_image(runner.home, runner.board)}
    return {}


def _image(runner_image=None):
    """The image the run's container ran when the tick named it (job.json `runner_image`, set
    when a strong runner started one); else the image the record resolves to."""
    if runner_image:
        return runner_image
    try:
        from capsule.runners import docker
        return docker.worker_image()
    except Exception:       # the pin is best effort; the rest of the record still stands
        return None


def _collect(workdir, role, route, run, runner_image=None):
    """Git runs through workspace._git: pinned config, explicit git dir, fast timeout. A diff that
    times out or exceeds DIFF_CAP gets a null hash and no patch file. The meter fields: `lane` and
    `batch_id` come from the run's metadata JSON, `reasoning_effort` only for the claude adapter,
    the one that applies it; each is null when unset."""
    ws = Path(workdir)
    route = route or {}
    meta = json.loads(run["metadata"] or "{}") if run else {}
    applied = (route.get("adapter") or route.get("provider")) == "claude"
    out = {"head_sha": workspace._git("rev-parse", "HEAD", ws=ws).stdout.strip(),
           "diff_sha256": None, "diff_bytes": None, "diff_file": None,
           "model": route.get("model"), "role": role, "runner_image": _image(runner_image),
           "reasoning_effort": route.get("reasoning_effort") if applied else None,
           "resumed_from": run["resumed_from"] if run else None,
           "batch_id": meta.get("batch_id"), "lane": meta.get("lane")}
    try:
        diff = workspace._git("diff", "--no-ext-diff", "--no-textconv", "HEAD", ws=ws).stdout
    except (subprocess.TimeoutExpired, RuntimeError) as exc:
        print(f"run_record: diff skipped: {exc}", file=sys.stderr)
        return None, out
    raw = diff.encode("utf-8", "surrogateescape")
    out["diff_bytes"] = len(raw)
    if raw and len(raw) <= DIFF_CAP:
        out.update(diff_sha256=hashlib.sha256(raw).hexdigest(), diff_file=DIFF_NAME)
        return raw, out
    return None, out


def record(spool_dir, workdir, role, route, run=None, runner_image=None):
    """Write start_state.json (and start_diff.patch, 0600, tracked-file changes only; untracked
    files are not captured). On any error log to stderr and return None so the run goes on."""
    try:
        raw, doc = _collect(workdir, role, route, run, runner_image)
        if raw:
            safe_write.atomic_write(spool_dir, Path(spool_dir) / DIFF_NAME, raw, mode=0o600)
        safe_write.atomic_write(spool_dir, Path(spool_dir) / NAME, json.dumps(doc))
        return doc
    except Exception as exc:
        print(f"run_record: skipped: {exc}", file=sys.stderr)
        return None
