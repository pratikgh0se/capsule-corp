"""M137 the detached builder behind `capsule image request-run --board B --task T --timeout S`
(started by `image_requests.step`, which hands it the machine's build lock on an inherited fd, so
the lock is held exactly as long as this process lives). It builds the clone under
`<home>/image/<task>/work` (`image_build.build`, label `capsule.kind=test`, `--timeout` bounds the
whole run: build plus every smoke line), reads the sizes, and writes `<home>/image/<task>.json`:
`{digest, reason, size, old_size, total}` in bytes. It records nothing and pushes nothing; the tick
step reads the file and decides. `old_size` is None when the board has no recorded image (growth
is then not measurable: there is no previous image to compare with). `total` sums `.Size` per
capsule image, which counts shared layers once per image: an overcount, so the 20 GB check errs
on the safe side."""
import json
import os
import tempfile

from capsule import image_build, paths
from capsule.runners import docker


def _sizes(name, digest, home):
    """{size, old_size, total} in bytes from docker, or {} when docker will not say."""
    def size(*refs):
        rc, out, _ = image_build._docker(["image", "inspect", "--format", "{{.Size}}", *refs], 60)
        got = out.split()
        return [int(x) for x in got] if rc == 0 and all(x.isdigit() for x in got) else None

    record = paths.image_record(home)
    old = [ln.strip().split("@", 1)[1] for ln in
           (record.read_text(encoding="utf-8").splitlines() if record.is_file() else [])
           if ln.strip().startswith(f"{name}@")]
    rc, ids, _ = image_build._docker(
        ["image", "ls", "--filter", "label=capsule=1", "-q", "--no-trunc"], 60)
    new, prior = size(digest), (size(old[-1]) if old else [None])
    every = size(*ids.split()) if ids.split() else [0]
    if rc or None in (new, every) or prior is None:
        return {}
    return {"size": new[0], "old_size": prior[0], "total": sum(every)}


def run(args):
    home = paths.home(args.capsule_home)
    name = docker.project_image_name(args.board)
    digest, reason = image_build.build(
        home / "image" / args.task / "work", args.board, home,
        tag=f"{name}:req-{args.task}", kind="test", total_s=args.timeout)
    res = {"digest": digest, "reason": reason, **(_sizes(name, digest, home) if digest else {})}
    fd, tmp = tempfile.mkstemp(dir=str(home / "image"))
    with os.fdopen(fd, "w") as fh:
        json.dump(res, fh)
    os.replace(tmp, home / "image" / f"{args.task}.json")
    return 0 if digest else 1
