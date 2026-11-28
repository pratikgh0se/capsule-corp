"""After-landing image sweep: untagged (dangling) images, and older tags of Capsule's own
images (capsule-base, capsule-python, capsule-web, capsule-corp-worker, and the `prev-*`
keep-tags of capsule-proj-<board>) that the current pins no longer name. Kept always: the
image the runner resolves, every digest in docker/IMAGES.lock and in the image record
($CAPSULE_HOME/images.lock), and any image a
container (running or stopped) uses. Removal never forces (no -f), by image id for an untagged
image and by tag for an old tag. A failed removal, or a docker that cannot answer, never raises:
it is reported once with the image name and the landing goes on. An untagged image that carries the
`capsule-layer` label is a rollback image. Per repo the sweep spares the newest one (newest `prev-*`
tag, else the newest labelled untagged image; image Created time, tie on id); older ones go.
Preview removes nothing."""
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events   # noqa: E402  docker_run(): the one door every maintenance script uses for docker
from capsule import paths   # noqa: E402  image_record()
from capsule.runners import docker as runner_docker   # noqa: E402  worker_image(): the resolver

DOCKER_DIR = Path(__file__).resolve().parents[1] / "docker"
OWN_REPOS = ("capsule-base", "capsule-python", "capsule-web", "capsule-corp-worker")
FRESH = timedelta(hours=2)        # an image this new may belong to a build or run in flight
TEST_TAG = "capsule-corp-worker:test"      # rebuilt and removed by tests/test_docker_image.sh
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _docker(run, *args):
    r = (run or events.docker_run)(list(args), capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed")
    return r.stdout


def _digests(path):
    try:
        return set(DIGEST.findall(Path(path).read_text()))
    except OSError:
        return set()


def _created(stamp):
    """UTC datetime of a Docker timestamp; one this cannot read counts as now (keeps the image)."""
    try:
        text = re.sub(r"(\.\d{6})\d*", r"\1", stamp.strip()).replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


def _info(run, ids):
    """{image id: (set of its id and RepoDigests digests, created datetime, labels dict)}."""
    if not ids:
        return {}
    out = json.loads(_docker(run, "image", "inspect", *ids))
    return {d["Id"]: ({d["Id"], *(r.split("@")[-1] for r in d.get("RepoDigests") or [])},
                      _created(d.get("Created", "")),
                      (d.get("Config") or {}).get("Labels") or {}) for d in out}


def worker_refs(docker_dir=DOCKER_DIR):
    """The worker image refs the runner would use: its own resolver (which honours
    OPS_WORKER_IMAGE, then the record)."""
    return {runner_docker.worker_image()} - {""}


def _proj_prev(repo, tag):
    """A keep-tag `capsule/image_build.py` made for a project image."""
    return repo.startswith("capsule-proj-") and tag.startswith("prev-")


def _rollback(rows, info):
    """Rows to spare: per repo the newest `prev-*` image; for a labelled untagged image, the newest
    per capsule-layer value unless a spared prev image already carries that value."""
    def newest(cands):
        return max(cands, key=lambda r: (info[r[0]][1], r[0]))
    rows = [tuple(r) for r in rows]
    spare, layers = set(), set()
    prev = {}
    for r in rows:
        if r[0] in info and (r[1] in OWN_REPOS or _proj_prev(r[1], r[2])) \
                and r[2].startswith("prev-"):
            prev.setdefault(r[1], []).append(r)
    for cands in prev.values():
        best = newest(cands)
        spare.add(best)
        layers.add(info[best[0]][2].get("capsule-layer"))
    untagged = {}
    for r in rows:
        lab = info.get(r[0], (0, 0, {}))[2]
        if r[1] == "<none>" and "capsule-layer" in lab:
            untagged.setdefault(lab["capsule-layer"], []).append(r)
    for value, cands in untagged.items():
        if value not in layers:
            spare.add(newest(cands))
    return spare


def plan(run=None, docker_dir=DOCKER_DIR, record=None, resolve=worker_refs):
    """[(target, label)] to remove: target is an image id (untagged) or a repo:tag."""
    pins = _digests(Path(docker_dir) / "IMAGES.lock") | _digests(record or paths.image_record())
    rows = [ln.split("\t") for ln in _docker(
        run, "image", "ls", "--no-trunc", "--format",
        "{{.ID}}\t{{.Repository}}\t{{.Tag}}").splitlines() if ln.count("\t") == 2]
    names = {f"{repo}:{tag}": i for i, repo, tag in rows if repo != "<none>"}
    keep = {names[r] for r in resolve(docker_dir) if r in names}     # whatever its tag
    keep.add(names.get(TEST_TAG))
    containers = _docker(run, "ps", "-a", "-q", "--no-trunc").split()
    if containers:
        keep |= set(_docker(run, "inspect", "--format", "{{.Image}}", *containers).split())
    info = _info(run, list(dict.fromkeys(i for i, _, _ in rows)))
    keep |= {i for i, (ds, made, _) in info.items()
             if ds & pins or made > datetime.now(timezone.utc) - FRESH}
    spare = _rollback(rows, info)
    out, seen = [], set()
    for i, repo, tag in rows:
        if i in keep:
            continue
        if (i, repo, tag) in spare:
            continue                                  # the one rollback image kept per repo
        if repo == "<none>" and i not in seen:
            out.append((i, f"<untagged> {i.removeprefix('sha256:')[:12]}"))
            seen.add(i)
        elif (repo in OWN_REPOS or _proj_prev(repo, tag)) and tag != "<none>" \
                and f"{repo}:{tag}" != TEST_TAG:
            out.append((f"{repo}:{tag}", f"{repo}:{tag} (older tag)"))
    return out


def sweep(apply, run=None, docker_dir=DOCKER_DIR, record=None, log=None, resolve=worker_refs):
    """Print the plan; with apply, remove each item without force. Returns the number removed
    (preview: the number listed). Never raises."""
    try:
        items = plan(run, docker_dir, record, resolve)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as e:
        print(f"image-sweep: docker could not answer ({e}); nothing removed", file=sys.stderr)
        return 0
    done, failed = 0, set()
    for target, label in items:
        if not apply:
            print(f"image-sweep: would remove {label}")
            continue
        try:
            ok = (run or events.docker_run)(["image", "rm", target], capture_output=True,
                                            text=True, timeout=120).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            done += 1
            print(f"image-sweep: removed {label}")
        elif target not in failed:
            failed.add(target)
            print(f"image-sweep: could not remove {label}; kept", file=sys.stderr)
            if log:
                log({"what": "image-rm-failed", "id": target, "before": None, "result": label})
    print(f"image-sweep: {'applied' if apply else 'preview only'}: {len(items)} listed"
          + (f", {done} removed" if apply else ", nothing removed"))
    return done if apply else len(items)
