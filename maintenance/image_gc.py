#!/usr/bin/env python3
"""Nightly image GC (R11, spec "Worker images" section 5), run by run.sh before the nightly session.

Scope: images carrying the label `capsule-layer` (any value) or `capsule=1`, plus the one legacy
tag capsule-corp-worker:test; any other image, and every intermediate layer (the listing has no
-a), is never listed, so never touched. Right before each removal the verdict is re-made from a
fresh inspect; if it now says keep, the image stays ("changed since listing"). Two age rules sit
on top (see AGE_KINDS and LEGACY_BEFORE): rule A removes a capsule=1 image whose capsule.kind is
test or run once it is over 24 hours old and unused (kind=image, or no kind, never goes by this
24-hour rule; a capsule-layer image of any kind still has the 14-day rule below); rule B, a
one-time sweep of leftovers from before labels, removes an untagged capsule-layer image or
exactly capsule-corp-worker:test (by that tag, an untag) created before LEGACY_BEFORE, last built
or tagged over 24 hours ago, unused, with no kind.
Kept: every digest in docker/IMAGES.lock and in the image record $CAPSULE_HOME/images.lock (what
`docker/build-base.sh` built on this machine; matched on the image id or a RepoDigests entry), per
layer the newest committed digest that differs from the lock's own (git log of the lock, newest
first, at most 100 commits: the previous image, so a rollback image survives a comment-only
commit, the rebuild of another layer, or a rebuild not committed yet; a layer whose history
runs out has none), any image that has a tag outside `capsule-*` (an image of
The owner's own built FROM a capsule layer inherits the label), any image with more than one tag,
any image a container (running or not) uses, and anything built or tagged within 14 days.
Docker records no pull or run time, so "used in the last 14 days" is an approximation: a container
exists, or the image is that new. A build time this cannot read keeps the image. If the lock's
git history cannot be read (no git, not a repository, lock not committed) nothing is removed.
The rest goes by `docker image rm <image id>`, never `-f`: Docker itself refuses an image a
container uses.

Dry-run is the default: it prints what it would remove and removes nothing. Only
OPS_IMAGE_GC_APPLY=1 (ops.env or env) removes. It never prunes volumes, never runs
`docker system prune`, and always ends with the `docker system df` report. A missing docker, an
unreadable lock or a failed listing removes nothing and still exits 0 (the nightly must not fail).
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events   # noqa: E402  docker_run(): the one door every maintenance script uses for docker
from capsule import paths   # noqa: E402  image_record(): what `docker/build-base.sh` built here

LABEL = "capsule-layer"
KEEP_DAYS = 14
# Rule A (shared with storage_gc.py): an object labelled capsule=1 and capsule.kind in AGE_KINDS
# that no container uses goes once it is older than AGE_LIMIT. kind=image (worker, base, python,
# web) and a capsule=1 object with no kind never go by this 24-hour rule; a capsule-layer image
# of any kind still falls under the 14-day rule (KEEP_DAYS) like any other.
AGE_KINDS, AGE_LIMIT = ("test", "run"), timedelta(hours=24)
# Rule B, one-time legacy sweep of leftovers made before labels existed: only what was created
# before this instant, so after it the sweep selects nothing.
LEGACY_BEFORE = datetime(2026, 11, 2, tzinfo=timezone.utc)
LEGACY_TAG = "capsule-corp-worker:test"     # rebuilt by tests/test_docker_image.sh, never pinned
LOCK = Path(__file__).resolve().parents[1] / "docker" / "IMAGES.lock"
HISTORY = 100      # newest commits of the lock the previous-digest walk will look at
LOCK_ROW = re.compile(r"^capsule-([a-z][a-z0-9-]*)@(sha256:[0-9a-f]{64})$")


def docker(*args):
    r = events.docker_run(list(args), capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed")
    return r.stdout


def when(stamp):
    """UTC datetime of a Docker timestamp (nanoseconds cut to micro). Unreadable counts as
    the far future, so a stamp this cannot read keeps the image (fail safe)."""
    try:
        text = re.sub(r"(\.\d{6})\d*", r"\1", stamp.strip()).replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return datetime.max.replace(tzinfo=timezone.utc)


def _rows(text):
    """{layer name: digest} for the capsule-<name>@sha256:... rows of a lock file."""
    found = [LOCK_ROW.match(line.strip()) for line in text.splitlines()]
    return {m.group(1): m.group(2) for m in found if m}


def _lock_digests(path):
    try:
        return set(_rows(Path(path).read_text()).values())
    except OSError:
        return set()


def _previous_digests(lock):
    """Per layer, the newest committed digest that differs from the lock file's own (walk the
    lock's git log newest first, at most HISTORY commits); a layer in the newest commit but not
    in the file counts as differing, so its committed digest is kept. A layer whose history
    runs out has no previous. Anything git cannot answer raises, so the caller removes nothing
    (a wrong 'previous' would delete a rollback image)."""
    now = _rows(lock.read_text())
    git = ["git", "-C", str(lock.parent)]
    log = subprocess.run(git + ["log", f"-n{HISTORY}", "--format=%H", "--", lock.name],
                         capture_output=True, text=True, timeout=60)
    shas = log.stdout.split()
    if log.returncode != 0 or not shas:
        raise RuntimeError(f"no git history for {lock}")
    out, waiting = set(), set(now)
    for n, sha in enumerate(shas):
        shown = subprocess.run(git + ["show", f"{sha}:./{lock.name}"],
                               capture_output=True, text=True, timeout=60)
        if shown.returncode != 0:
            raise RuntimeError(f"git cannot show {lock} at {sha[:12]}")
        rows = _rows(shown.stdout)
        if n == 0:
            waiting |= set(rows)    # a layer the working tree dropped keeps its committed digest
        for name, dig in rows.items():
            if name in waiting and dig != now.get(name):
                out.add(dig)
                waiting.discard(name)
        if not waiting:
            break
    return out


def _record(d):
    """The record every verdict is made from, built from one `docker image inspect` entry."""
    created = when(d.get("Created", ""))
    tagged = (d.get("Metadata") or {}).get("LastTagTime")
    return {"id": d["Id"], "labels": (d.get("Config") or {}).get("Labels") or {},
            "size": d.get("Size", 0), "refs": d.get("RepoTags") or [], "created": created,
            "digests": d.get("RepoDigests") or [],
            "built": max(created, when(tagged)) if tagged else created}


def _labelled_images():
    """[record] for every capsule-layer or capsule=1 image (and the legacy tag), one per image
    id. No -a: only top-level images are ever candidates (Docker hides an untagged one unless
    asked for dangling=true); an intermediate layer, which Docker refuses to remove without
    force, is never listed."""
    ids = []
    for extra in ((), ("--filter", "dangling=true")):
        for flt in (f"label={LABEL}", "label=capsule=1", f"reference={LEGACY_TAG}"):
            ids += docker("image", "ls", "-q", "--no-trunc", *extra, "--filter", flt).split()
    if not ids:
        return []
    out = [_record(d) for d in json.loads(docker("image", "inspect", *dict.fromkeys(ids)))]
    return [r for r in out if LABEL in r["labels"] or r["labels"].get("capsule") == "1"
            or LEGACY_TAG in r["refs"]]


def _used_ids():
    """Image ids that any container (running or stopped) was made from."""
    names = docker("ps", "-a", "-q", "--no-trunc").split()
    if not names:
        return set()
    return set(docker("inspect", "--format", "{{.Image}}", *names).split())


def _has(img, digests):
    """True when the image id, or one of its RepoDigests entries, is one of the digests."""
    return img["id"] in digests or any(d.split("@")[-1] in digests for d in img["digests"])


def _legacy_image(img, now):
    """ONE-TIME legacy sweep (rule B): an untagged (top-level dangling) capsule-layer image, or
    exactly LEGACY_TAG, created before LEGACY_BEFORE, last built or tagged over AGE_LIMIT ago,
    with no capsule.kind. Delete this function (and its calls here) once a run shows 0 legacy
    items; after LEGACY_BEFORE it selects nothing."""
    old = img["created"] < LEGACY_BEFORE and img["built"] <= now - AGE_LIMIT
    ours = img["refs"] == [LEGACY_TAG] or (not img["refs"] and LABEL in img["labels"])
    return old and ours and "capsule.kind" not in img["labels"]


def _aged_out(img, now):
    """Rule A: capsule=1 with a kind in AGE_KINDS, over AGE_LIMIT old (in-use and lock checks
    come first in _keep_reason)."""
    labels = img["labels"]
    return (labels.get("capsule") == "1" and labels.get("capsule.kind") in AGE_KINDS
            and img["built"] <= now - AGE_LIMIT)


def _keep_reason(img, locked, previous, used, now):
    """Why an image stays, or None to remove it (the order is the order of the report)."""
    if _has(img, locked):
        return "lock"
    if _has(img, previous):
        return "previous"
    if any(not tag.startswith("capsule-") for tag in img["refs"]):
        return "tag outside capsule-*"
    if len(img["refs"]) > 1:
        return f"{len(img['refs'])} tags"
    if img["id"] in used:
        return "in use"
    if _aged_out(img, now) or _legacy_image(img, now):
        return None
    if LABEL not in img["labels"]:      # listed for capsule=1 only: never by the 14-day rule
        return "no age rule applies"
    return "recent" if img["built"] >= now - timedelta(days=KEEP_DAYS) else None


def _recheck(img, target, locked, previous):
    """The listing's own question asked again on fresh facts (the object re-inspected by `target`,
    containers re-read, clock re-read): None when it still says remove, else why not."""
    r = events.docker_run(["image", "inspect", target], capture_output=True, text=True,
                          timeout=120)
    found = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    if not found or found[0]["Id"] != img["id"]:
        return "gone or moved"
    return _keep_reason(_record(found[0]), locked, previous, _used_ids(),
                        datetime.now(timezone.utc))


def _remove(img, locked, previous):
    """None when removed, else why not. By image id, never by tag (a tag removal would only untag
    and keep the layers), except LEGACY_TAG, removed by that exact tag, an untag, never forced.
    Just before, the verdict is re-made from a fresh inspect of the same target."""
    target = LEGACY_TAG if img["refs"] == [LEGACY_TAG] else img["id"]
    why = _recheck(img, target, locked, previous)
    if why:
        return f"changed since listing: {why}"
    ok = events.docker_run(["image", "rm", target], capture_output=True,
                           timeout=120).returncode == 0
    return None if ok else "FAILED"


def _run(lock, apply):
    locked = _lock_digests(lock)
    if not locked:
        print(f"image-gc: {lock} has no digest rows, nothing removed")
        return 0
    previous = _previous_digests(lock)
    locked |= _lock_digests(paths.image_record())   # a tester's own build: hours of work, kept
    used, now = _used_ids(), datetime.now(timezone.utc)
    verdicts = [(img, _keep_reason(img, locked, previous, used, now))
                for img in _labelled_images()]
    verb = "remove" if apply else "would remove"
    gone = 0
    for img, reason in verdicts:
        label = " ".join(img["refs"]) or "<untagged>"
        label += " [legacy]" if not reason and _legacy_image(img, now) else ""
        short = img["id"].removeprefix("sha256:")[:12]
        if reason:
            print(f"keep {short} {label} ({reason})")
            continue
        why = _remove(img, locked, previous) if apply else None
        if why and why != "FAILED":
            print(f"keep {short} {label} ({why})")
            continue
        print(f"{verb} {short} {label} ({img['size']} bytes)" + (" FAILED" if why else ""))
        gone += not why
    mode = "applied" if apply else "dry-run, nothing removed; OPS_IMAGE_GC_APPLY=1 removes"
    print(f"image-gc: {mode}: {gone} to remove or removed, {len(verdicts) - gone} kept or failed")
    print(docker("system", "df"))
    return 0


def main():
    apply = os.environ.get("OPS_IMAGE_GC_APPLY") == "1"      # exactly "1", no trimming
    try:
        return _run(Path(os.environ.get("IMAGE_GC_LOCK", LOCK)), apply)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as e:
        print(f"image-gc: {e}; nothing removed", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
