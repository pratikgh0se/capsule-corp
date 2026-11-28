"""Shared fakes for test_storage_gc.py and test_storage_gc_tmp.py: the docker CLI as argv over two
lists (one patched seam in storage_gc, never real docker) and a base TestCase that runs
storage_gc.main against it with a throwaway TMPDIR, HOME and state dir."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402

tmpclean.install()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import storage_gc  # noqa: E402

H64 = "ab" * 32
REAL_IMAGES = storage_gc._images
REAL_TMP_ROOTS = storage_gc.TMP_ROOTS
NO_SUCH = "Error response from daemon: No such object"


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def box(cid, name, status="exited", labels=None, hours=5, volumes=()):
    return {"Id": cid * 64, "Name": "/" + name, "Created": ago(hours=hours + 1),
            "State": {"Status": status, "FinishedAt": ago(hours=hours)},
            "Config": {"Labels": {"capsule": "1"} if labels is None else labels},
            "SizeRw": 1000, "Mounts": [{"Type": "volume", "Name": v} for v in volumes]}


def vol(name, labels=None, days=5):
    return {"Name": name, "Labels": labels, "CreatedAt": ago(days=days)}


class FakeDocker:
    """`ls_ignores_dangling` lets a test prove the script's own mounted-volume check, not just
    Docker's dangling filter."""
    def __init__(self, boxes=(), vols=(), ls_ignores_dangling=False):
        self.boxes, self.vols, self.calls = list(boxes), list(vols), []
        self.all_vols = ls_ignores_dangling

    def __call__(self, *a):
        self.calls.append(a)
        out, rc, err = "", 0, ""
        flat = " ".join(a)
        mounted = {m["Name"] for b in self.boxes for m in b["Mounts"]}
        if a[0] == "ps":
            want = [x.split("=", 1) for x in a if "=" in x and not x.startswith("--")]
            for b in self.boxes:
                ok = all((k == "label" and v.split("=")[0] in b["Config"]["Labels"]) or
                         (k == "volume" and v in [m["Name"] for m in b["Mounts"]])
                         for k, v in want)
                out += b["Id"] + "\n" if ok else ""
        elif flat.startswith("container inspect"):
            ids = [x for x in a[2:] if not x.startswith("-")]
            found = [b for b in self.boxes if b["Id"] in ids]
            out, rc, err = json.dumps(found), 0 if found else 1, "" if found else NO_SUCH
        elif flat.startswith("volume ls"):
            out = "".join(v["Name"] + "\n" for v in self.vols
                          if self.all_vols or v["Name"] not in mounted)
        elif flat.startswith("volume inspect"):
            found = [v for v in self.vols if v["Name"] in a[2:]]
            out, rc, err = json.dumps(found), 0 if found else 1, "" if found else NO_SUCH
        elif flat.startswith("system df"):
            out = ("Local Volumes space usage:\n\nVOLUME NAME   LINKS   SIZE\n" +
                   "".join(f"{v['Name']}   0   2.5MB\n" for v in self.vols))
        elif a[0] == "rm":
            hit = [b for b in self.boxes if b["Id"] == a[1]]
            if hit and hit[0]["State"]["Status"] != "running":
                self.boxes.remove(hit[0])
            else:
                rc = 1
        elif flat.startswith("volume rm"):
            hit = [v for v in self.vols if v["Name"] == a[2] and a[2] not in mounted]
            self.vols = [v for v in self.vols if v not in hit]
            rc = 0 if hit else 1
        else:
            rc = 99
        return subprocess.CompletedProcess(list(a), rc, out, err)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="storage-gc-test-")).resolve()
        self.tmpdir = self.tmp / "T"
        self.tmpdir.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.tmp / "state"
        env = {"AGENT_OPS_STATE_DIR": str(self.state), "CLEANUP_TMPDIR": str(self.tmpdir),
               "HOME": str(self.home)}
        self.enterContext(mock.patch.dict(os.environ, env))
        # the guard accepts macOS temp roots only; here the throwaway dir is the temp root (a Linux
        # container's is /tmp). REAL_TMP_ROOTS keeps the production value for the guard test.
        self.enterContext(mock.patch.object(storage_gc, "TMP_ROOTS", (str(self.tmp) + "/",)))
        self.addCleanup(self._clean)
        # the one-time legacy sweep is off unless a test turns it on, so these tests do not depend
        # on today's date; the real LEGACY_BEFORE is pinned in test_storage_gc_ages.py
        self.enterContext(mock.patch.object(storage_gc, "LEGACY_BEFORE",
                                            datetime(2000, 1, 1, tzinfo=timezone.utc)))
        self.images = self.enterContext(mock.patch.object(storage_gc, "_images",
                                                          return_value=False))

    def _clean(self):
        subprocess.run(["chmod", "-R", "u+rwx", str(self.tmp)], check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def gc(self, fake, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(storage_gc, "_try", fake), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = storage_gc.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def log(self):
        path = self.state / "storage_gc.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def fleet(self):
        return FakeDocker([
            box("1", "capsule-w-old"),                                     # chosen
            box("2", "capsule-w-created", status="created", hours=3),      # chosen (Created)
            box("3", "capsule-w-young", hours=0),                          # under 1 hour
            box("4", "capsule-w-running", status="running", hours=9),      # never running
            box("5", "capsule-corp-grafana", labels={"capsule": "1"}),     # compose stack name
            box("6", "capsule-w-compose", labels={"capsule": "1",
                                                  "com.docker.compose.project": "x"}),
            box("7", "someone-elses", labels={}),                          # no label
        ])

    def mk(self, name, hours=48, inner_hours=None):
        d = self.tmpdir / name
        d.mkdir()
        f = d / "file"
        f.write_text("x" * 100)
        for p, h in ((f, inner_hours or hours), (d, hours)):
            os.utime(p, (time.time() - h * 3600,) * 2)
        return d
