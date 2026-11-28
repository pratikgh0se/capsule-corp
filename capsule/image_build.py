"""`python3 -m capsule image build --project DIR --board B [--dry-run]` and `image which --board B`
(INFRA-10, worker-images spec §1-2 steps 3-5, no builder loop). `build` renders the project layer
(`image_render.render`, the same judge as `image render`, FROM the machine's record rows), runs
`docker build` in a minimal context (a temp dir with only the files the rendered recipe COPYs,
never DIR itself: .env, .git, keys and the vault never reach the daemon), reads the
digest, smoke-tests every `smoke` line in a `--network=none` container started from that
digest, and only then rewrites the board's one
`capsule-proj-B@sha256:<hex>` row of $CAPSULE_HOME/images.lock (atomic, under a lock, other rows
kept as they are). Exit 0 recorded, 1 refused or failed (record untouched), 2 bad board name or
config. `which` prints the image the docker runner starts for the board: `runners/docker.py`
`worker_image`, the one function the runner itself calls. Docker comes from
`docker_door` (AGENT_OPS_DOCKER_BIN is a folder holding `docker`; a file there is used as is)."""
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from capsule import config, docker_door, image_render, paths
from capsule.runners import docker

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
BUILD_S, SMOKE_S, INSPECT_S = 1200, 300, 60
LOCK_TRIES, LOCK_WAIT = 600, 0.2     # build-base.sh: 600 tries 0.2 s apart = 2 minutes
REPO_NAME = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")   # a docker repository name
FROM_LOCAL = re.compile(r"^FROM (capsule-[a-z0-9-]+)@(sha256:[0-9a-f]{64})\s*$", re.M)
ANY_FROM_LOCAL = re.compile(r"^\s*from\s+(?:--\S+\s+)*capsule-\S*@sha256:", re.I | re.M)
COPY_LINE = re.compile(r"COPY (.+) \S+$")
ADD_COPY = re.compile(r"\s*(ADD|COPY)\b", re.I)
CONTEXT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")     # a bare file name: no dotfile, no path
SECRET_NAME = re.compile(r"\.env|\.pem|\.key|\.p12|\.pfx|^id_|secret|credential", re.I)
SMOKE_ARGS = ("--rm", "--network=none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges")


def _say(text):
    print(f"capsule image: {text}", file=sys.stderr)


def _docker(args, timeout, text=None):
    """(returncode, stdout, stderr); a missing docker or a timeout is rc 1 with the reason."""
    bin_ = os.environ.get("AGENT_OPS_DOCKER_BIN", "")
    exe = bin_ if bin_ and Path(bin_).is_file() else "docker"
    try:
        done = docker_door.docker_run(args, docker=exe, capture_output=True, text=True,
                                      timeout=timeout, input=text)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"docker {args[0]}: {exc}"
    return done.returncode, done.stdout, done.stderr


def _record(path, name, row):
    """Rewrite `path` with `row` as the only `name@` row, every other line kept in order. Under
    the lock `docker/build-base.sh` takes: the `<path>.lockdir` mkdir lock (macOS has no flock
    binary, so that is the one both writers share), retried for 2 minutes, never taken over,
    removed on exit; plus flock on `<path>.lock`, which the script uses where the host has it.
    OSError (lock held too long, a write that fails) leaves the record as it was."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lockdir = Path(f"{path}.lockdir")
    for _ in range(LOCK_TRIES):
        try:
            lockdir.mkdir()
            break
        except FileExistsError:
            time.sleep(LOCK_WAIT)
    else:
        raise TimeoutError(f"{lockdir} still held after 2 minutes; the image was built, rerun "
                           "this to record it (remove that folder if no build is running)")
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(f"{path}.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                kept = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                        if not ln.strip().startswith(f"{name}@")]
            except OSError:
                kept = []
            tmp.write_text("".join(f"{ln}\n" for ln in [*kept, row]), encoding="utf-8")
            os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
        lockdir.rmdir()


def _copy_plain(src_path, dest_path, problem):
    """Copy one regular, single-link file via an O_NOFOLLOW fd, else ValueError(problem)."""
    try:
        fd = os.open(src_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ValueError(problem) from None
    with os.fdopen(fd, "rb") as src:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise ValueError(problem)
        with open(dest_path, "wb") as out:
            shutil.copyfileobj(src, out)


def _stage(project, dockerfile, dest):
    """Copy into `dest` just the files `dockerfile` COPYs from `project`. ValueError (refused)
    for an ADD/COPY line it cannot read, or a name that is a path, a dotfile, secret-looking, a
    symlink, a hard link or not a regular file (opened O_NOFOLLOW, checked on the open fd)."""
    lines = dockerfile.splitlines()
    for line in lines:
        if ADD_COPY.match(line) and not COPY_LINE.match(line):
            raise ValueError(f"the recipe line {line.strip()!r} is not a plain COPY of files; "
                             "not sent to docker")
    names = [n for m in map(COPY_LINE.match, lines) if m for n in shlex.split(m.group(1))]
    for name in names:
        problem = (f"the recipe COPYs {name!r}, which is not a plain non-secret file "
                   "in the project; not sent to docker")
        if not CONTEXT_NAME.fullmatch(name) or SECRET_NAME.search(name):
            raise ValueError(problem)
        _copy_plain(Path(project) / name, Path(dest) / name, problem)


def _contexts(text):
    """(`--build-context` args, problem) for each `FROM capsule-X@sha256:D` of the recipe. BuildKit
    (OrbStack's engine) cannot resolve a digest-pinned FROM of a local-only image (it asks
    docker.io), so each pinned ref is mapped to a local tag of that image (`capsule-X:local` if it
    has it, else any other tag): found by the digest ref itself, so a drifted or missing `:local`
    tag does not matter, only that the digest is local. An untagged image gets no context (a
    containerd store may still resolve it) and a note; a digest that is not local is refused. The
    recipe text is unchanged: the digest stays in FROM. Without buildx (the legacy builder
    resolves local digests itself, and has no such flag) no context is passed."""
    refs = list(dict.fromkeys(FROM_LOCAL.findall(text)))
    unread = len(ANY_FROM_LOCAL.findall(text)) != len(FROM_LOCAL.findall(text))
    if not (refs or unread) or _docker(["buildx", "version"], INSPECT_S)[0]:
        return [], ""
    if unread:
        return [], ("a FROM of a pinned capsule image is not in the form the build-context mapping "
                    "reads: write `FROM capsule-X@sha256:<digest>` alone on its line")
    args = []
    for name, digest in refs:
        rc, out, _ = _docker(["image", "inspect", "--format", '{{.Id}} {{join .RepoTags ","}}',
                              f"{name}@{digest}"], INSPECT_S)
        found, _, tags = out.strip().partition(" ")
        if rc or found != digest:
            return [], (f"{name}@{digest} is not a local image; "
                        f"run docker/build-base.sh {name.removeprefix('capsule-')}")
        tags = [tag for tag in tags.split(",") if tag]
        if not tags:
            _say(f"note: {name}@{digest} has no local tag, no build context passed")
            continue
        tag = f"{name}:local" if f"{name}:local" in tags else tags[0]
        args += ["--build-context", f"{name}@{digest}=docker-image://{tag}"]
    return args, ""


def _smoke(ref, line):
    return ["run", *SMOKE_ARGS, ref, "sh", "-c", line]


def _refused(project, record):
    """(Dockerfile, smoke lines, []) or (None, [], [reason]) as `image render` judges."""
    if not record.is_file():
        return None, [], [f"no image record at {record}: run docker/build-base.sh python first"]
    try:
        image, rows = image_render._read(project, record)
    except (OSError, ValueError) as exc:
        return None, [], [f"cannot read the manifest or the record: {exc}"]
    text, problems = image_render.render(image, project, rows)
    return text, image.get("smoke", []), problems


def _keep_tag(tag, cap):
    """Give the image `tag` names now keeps a second name, `<repo>:prev-<12 hex of its id>`, so the
    build below, which moves `tag`, never leaves it untagged while the record still names it
    (inc_3a7d6457). Returns "" (also when `tag` names nothing yet) or the reason the lookup or
    tag failed."""
    rc, out, err = _docker(["image", "ls", "--no-trunc", "-q", tag], cap)
    if rc:      # a failed probe is not "absent": stop (fail closed)
        return f"could not look up {tag}: {err.strip()[-200:]}"
    old = out.split()[0] if out.split() else ""
    if not DIGEST.fullmatch(old):
        return ""
    keep = f"{tag.rsplit(':', 1)[0]}:prev-{old[7:19]}"
    rc, _, err = _docker(["tag", old, keep], cap)
    return f"could not keep-tag {tag} as {keep}: {err.strip()[-200:]}" if rc else ""


def build(project, board, home, *, tag=None, kind="image", total_s=None):
    """(digest, reason): build and smoke-test the board's project layer, record nothing. `digest`
    is None with the reason when the render judge refused or a docker step failed (each reason is
    also said on stderr). `tag` and `kind` (the capsule.kind label) default to the board's own
    `:local` image; an image request builds under its own tag and kind `test`. `total_s` bounds
    the whole run (build, inspect and every smoke line share it); without it each step has
    its own."""
    left = (lambda cap: cap) if total_s is None else (
        lambda cap, end=time.monotonic() + total_s: max(1, min(cap, int(end - time.monotonic()))))
    name = docker.project_image_name(board)
    tag = tag or f"{name}:local"
    text, smoke, problems = _refused(project, paths.image_record(home))
    if problems:
        for problem in problems:
            _say(f"refused: {problem}")
        return None, "refused: " + "; ".join(problems)
    ctx_args, problem = _contexts(text)
    if problem:
        _say(f"refused: {problem}")
        return None, f"refused: {problem}"
    cmd = ["build", "--pull=false", "--provenance=false", "--sbom=false",
           "--label", "capsule=1", "--label", f"capsule.kind={kind}",
           *ctx_args, "-t", tag, "-f", "-"]
    with tempfile.TemporaryDirectory(prefix="capsule-image-ctx-") as ctx:
        try:
            _stage(project, text, ctx)
        except (OSError, ValueError) as exc:
            _say(f"refused: {exc}")
            return None, f"refused: {exc}"
        problem = _keep_tag(tag, left(INSPECT_S))
        if problem:
            _say(problem)
            return None, problem
        rc, _, err = _docker([*cmd, ctx], left(BUILD_S), text)
    if rc:
        _say(f"docker build failed: {err.strip()[-500:]}")
        return None, f"docker build failed: {err.strip()[-300:]}"
    rc, out, err = _docker(["image", "inspect", "--format", "{{.Id}}", tag], left(INSPECT_S))
    digest = out.strip()
    if rc or not DIGEST.fullmatch(digest):
        _say(f"bad image digest {digest!r} {err.strip()[-200:]}")
        return None, f"bad image digest {digest!r}"
    for line in smoke:
        rc, _, err = _docker(_smoke(f"{name}@{digest}", line), left(SMOKE_S))
        if rc:
            _say(f"smoke line failed: {line!r}: {err.strip()[-300:]}")
            return None, f"smoke line failed: {line!r}"
    return digest, ""


def _build(args):
    name = docker.project_image_name(args.board)
    home = paths.home(args.capsule_home)
    record = paths.image_record(home)
    if args.dry_run:
        text, smoke, problems = _refused(args.project, record)
        if problems:
            for problem in problems:
                _say(f"refused: {problem}")
            return 1
        with tempfile.TemporaryDirectory(prefix="capsule-image-ctx-") as ctx:
            try:
                _stage(args.project, text, ctx)
            except (OSError, ValueError) as exc:
                _say(f"refused: {exc}")
                return 1
        tag = f"{name}:local"
        build_cmd = ["build", "--pull=false", "--provenance=false", "--sbom=false",
                     "--label", "capsule=1", "--label", "capsule.kind=image",
                     "-t", tag, "-f", "-", "<minimal-context>"]
        inspect = ["image", "inspect", "--format", "{{.Id}}", tag]
        ref = f"{name}@sha256:<digest>"
        cmds = [build_cmd, inspect, *(_smoke(ref, line) for line in smoke)]
        print("\n".join("docker " + shlex.join(argv) for argv in cmds))
        for name, pinned in dict.fromkeys(FROM_LOCAL.findall(text)):   # a dry run runs no docker
            print(f"(a real build adds --build-context {name}@{pinned}=docker-image://<local tag> "
                  "after checking the image is local)")
        print(f"would record {ref} in {record}")
        return 0
    digest, _ = build(args.project, args.board, home)
    if digest is None:
        return 1
    try:
        _record(record, name, f"{name}@{digest}")
    except OSError as exc:
        _say(f"cannot record the image in {record}: {exc}")
        return 1
    print(f"{name}@{digest}")
    return 0


def _which(args):
    home = paths.home(args.capsule_home)
    try:
        cfg = json.loads(paths.resolve_config(home, args.board).read_text(encoding="utf-8"))
        loop = config.redesign(cfg)["image_loop"]
    except (OSError, ValueError, TypeError, AttributeError) as exc:   # not a config object
        _say(f"no usable config for board {args.board!r}: {exc}")
        return 2
    if loop != config.IMAGE_RECORD:
        _say(f'config error: redesign.image_loop must be "{config.IMAGE_RECORD}"')
        return 2
    image, source = docker.worker_image_source(home, args.board)
    print(image)
    _say(f"source: {source}")
    if not image:
        _say(docker.missing_image_message(args.board))
    return 0 if image else 1


def main(args):
    problem = paths.board_name_problem(args.board)
    if problem:
        _say(problem)
        return 2
    image = docker.project_image_name(args.board)
    if args.image_cmd in ("build", "request-run") and not REPO_NAME.fullmatch(image):
        _say(f"{image!r} is not a docker repository name (letters, digits, and single "
             ". or _ or - between them); pick another board name")
        return 2
    if args.image_cmd == "request-run":       # M137: a tick step's detached builder
        from capsule import image_request_run
        return image_request_run.run(args)
    return (_build if args.image_cmd == "build" else _which)(args)
