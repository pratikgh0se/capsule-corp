"""Shared fakes for the R11 tests (test_image_gc.py, test_web_stack.py): a fake `docker` on PATH
whose image store is a JSON file, image fixtures, and a base TestCase that runs image_gc.py
against them. Nothing here touches the real docker."""
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GC = ROOT / "maintenance" / "image_gc.py"
N_IMAGES = 14      # what scenario() holds

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_DOCKER_STATE"]
args = sys.argv[1:]
with open(state + ".calls", "a") as f:
    f.write(" ".join(args) + "\n")
db = json.load(open(state))
imgs, boxes = db["images"], db["containers"]
def save():
    json.dump(db, open(state, "w"))
if args[:2] == ["image", "ls"]:
    want = [a.split("=", 1) for a in args if a.startswith(("label=", "reference="))]
    def hit(i, kind, text):
        if kind == "reference":
            return text in i["RepoTags"]
        key, _, val = text.partition("=")
        labels = i["Config"].get("Labels") or {}
        return key in labels and val in ("", labels[key])
    dangling = "dangling=true" in args
    for i in imgs:
        if "-a" not in args and (i.get("Intermediate") or not (i["RepoTags"] or dangling)):
            continue    # like docker: no -a hides parent layers, and untagged ones unless dangling
        if all(hit(i, k, v) for k, v in want) and not (dangling and i["RepoTags"]):
            print(i["Id"])
elif args[:2] == ["image", "inspect"]:
    found = [i for i in imgs if i["Id"] in args[2:] or set(i["RepoTags"]) & set(args[2:])]
    print(json.dumps(found))
    sys.exit(0 if found else 1)         # by id or by tag, like docker
elif args[:1] == ["ps"]:
    for n, _ in enumerate(boxes):
        print("box%d" % n)
elif args[:1] == ["inspect"]:
    for name in args[3:]:
        print(boxes[int(name[3:])])
elif args[:2] == ["image", "rm"]:
    ref = args[2]
    for i in list(imgs):
        if ref in i["RepoTags"] or ref == i["Id"]:
            if i["Id"] in boxes:
                sys.exit(1)
            if ref == i["Id"]:          # by id: every tag goes and the image with them
                imgs.remove(i)
            else:
                i["RepoTags"].remove(ref)
                if not i["RepoTags"]:
                    imgs.remove(i)
            save()
            sys.exit(0)
    sys.exit(1)
elif args[:2] == ["system", "df"]:
    print("TYPE   TOTAL   fake-df-report")
else:
    sys.exit(99)
'''


def digest(ch):
    return "sha256:" + ch * 64


def image(ch, layer, age_days, tags=(), digests=(), labels=None):
    """`labels` adds to (or, with layer None, is) the label set, e.g. {"capsule": "1"}."""
    when = datetime.now(timezone.utc) - timedelta(days=age_days)
    labels = {**({"capsule-layer": layer} if layer else {}), **(labels or {})}
    return {"Id": digest(ch), "RepoTags": list(tags), "RepoDigests": list(digests),
            "Created": when.strftime("%Y-%m-%dT%H:%M:%S.123456789Z"),
            "Metadata": {"LastTagTime": "0001-01-01T00:00:00Z"},
            "Config": {"Labels": labels}, "Size": 1000}


def script(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def scenario():
    """base: A lock, E only in the previous lock (git), F recent, B and C old, D old but in a
    container, M tagged myapp:dev (the owner's own image FROM a capsule layer), B also carries one
    capsule tag; python: P lock, Q previous lock, R old, N two capsule tags; web: S is kept only
    through its RepoDigests entry (lock digest 7), T is newer than S and in no lock; U has no
    label."""
    return [
        image("a", "base", 40, ["capsule-base:local"]),
        image("e", "base", 45), image("f", "base", 0.5),
        image("b", "base", 50, ["capsule-base:old"]), image("c", "base", 90),
        image("d", "base", 100), image("m", "base", 60, ["myapp:dev"]),
        image("1", "stack-python", 60, ["capsule-python:local"]),
        image("2", "stack-python", 70), image("3", "stack-python", 80),
        image("n", "stack-python", 65, ["capsule-python:x1", "capsule-python:x2"]),
        image("5", "stack-web", 30, [], ["capsule-web@" + digest("7")]),
        image("6", "stack-web", 20),
        image("u", None, 200, ["redis:7-alpine"]),
    ]


class Fake(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="web-stack-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.state = self.tmp / "store.json"
        self.lock = self.tmp / "IMAGES.lock"
        script(self.bin / "docker", FAKE_DOCKER)

    def store(self, images, boxes=()):
        self.state.write_text(json.dumps({"images": images, "containers": list(boxes)}))

    def left(self):
        return {i["Id"][7] for i in json.loads(self.state.read_text())["images"]}

    def calls(self):
        log = Path(str(self.state) + ".calls")
        return log.read_text().splitlines() if log.exists() else []

    def gc(self, switch=None):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_DOCKER_STATE": str(self.state),
               "AGENT_OPS_DOCKER_BIN": str(self.bin), "IMAGE_GC_LOCK": str(self.lock),
               "CAPSULE_HOME": str(self.tmp / "capsule-home")}   # not the live image record
        if switch is not None:
            env["OPS_IMAGE_GC_APPLY"] = switch
        return subprocess.run([sys.executable, str(GC)], env=env, capture_output=True, text=True)
