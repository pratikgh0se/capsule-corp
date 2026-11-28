"""maintenance/image_sweep.py against a fake docker: never real docker."""
import contextlib
from datetime import datetime, timezone
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import image_sweep   # noqa: E402


def D(c):
    return "sha256:" + c * 64


PIN, OLD, DANG, USED, WORKER, OTHER = D("a"), D("b"), D("c"), D("d"), D("e"), D("f")
TEST, NEW = D("7"), D("8")
NOW = datetime.now(timezone.utc).isoformat()


class FakeDocker:
    def __init__(self, rm_fail=()):
        self.calls, self.rm_fail = [], set(rm_fail)
        self.images = [  # id, repo, tag, repo digest
            (PIN, "capsule-python", "local", PIN), (OLD, "capsule-python", "old", D("1")),
            (DANG, "<none>", "<none>", None), (USED, "capsule-web", "prev", D("2")),
            (WORKER, "capsule-corp-worker", "cur", None),
            (OTHER, "mine", "x", None), (TEST, "capsule-corp-worker", "test", None),
            (NEW, "capsule-web", "fresh", None)]

    def __call__(self, args, **kw):
        self.calls.append(args)
        out, rc = "", 0
        if args[:2] == ["image", "ls"]:
            out = "".join(f"{i}\t{r}\t{t}\n" for i, r, t, _ in self.images)
        elif args[0] == "ps":
            out = "cont1\n"
        elif args[0] == "inspect":
            out = USED + "\n"
        elif args[:2] == ["image", "inspect"]:
            out = json.dumps([{"Id": i, "RepoDigests": [f"x@{d}"] if d else [],
                               "Created": NOW if i == NEW else "2020-01-01T00:00:00Z"}
                              for i, _, _, d in self.images])
        elif args[:2] == ["image", "rm"]:
            rc = 1 if args[2] in self.rm_fail else 0
        return subprocess.CompletedProcess(args, rc, out, "")

    def removed(self):
        return [c[2] for c in self.calls if c[:2] == ["image", "rm"]]


class Sweep(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "IMAGES.lock").write_text(f"capsule-python@{PIN}\n")
        self.rec = self.tmp / "none.lock"
        self.resolve = lambda d: {"capsule-corp-worker:cur"}

    def go(self, apply, fake, log=None):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
            n = image_sweep.sweep(apply, fake, self.tmp, self.rec, log, self.resolve)
        return n, out.getvalue(), err.getvalue()

    def test_plan_keeps_pins_used_and_foreign(self):
        got = [t for t, _ in image_sweep.plan(FakeDocker(), self.tmp, self.rec, self.resolve)]
        self.assertEqual(got, ["capsule-python:old", DANG])

    def test_preview_removes_nothing_lists_both(self):
        f = FakeDocker()
        n, out, _ = self.go(False, f)
        self.assertEqual((n, f.removed()), (2, []))
        self.assertIn("would remove capsule-python:old", out)
        self.assertIn("<untagged>", out)

    def test_apply_removes_without_force(self):
        f = FakeDocker()
        n, _, _ = self.go(True, f)
        self.assertEqual((n, f.removed()), (2, ["capsule-python:old", DANG]))
        self.assertFalse(any("-f" in c or "--force" in c for c in f.calls))

    def test_failed_removal_logged_once_never_raises(self):
        f, rows = FakeDocker(rm_fail=[DANG]), []
        n, _, err = self.go(True, f, rows.append)
        self.assertEqual(n, 1)
        self.assertEqual(err.count("could not remove"), 1)
        self.assertEqual([r["what"] for r in rows], ["image-rm-failed"])

    def test_docker_down_removes_nothing(self):
        def down(args, **kw):
            raise FileNotFoundError("docker")
        n, _, err = self.go(True, down)
        self.assertEqual(n, 0)
        self.assertIn("nothing removed", err)

    def test_resolver_pin_kept_whatever_its_tag(self):
        self.resolve = lambda d: {"capsule-python:old"}      # e.g. OPS_WORKER_IMAGE
        got = [t for t, _ in image_sweep.plan(FakeDocker(), self.tmp, self.rec, self.resolve)]
        self.assertEqual(got, [DANG, "capsule-corp-worker:cur"])   # only the resolved one stays

    def test_test_tag_and_fresh_images_skipped(self):
        got = [t for t, _ in image_sweep.plan(FakeDocker(), self.tmp, self.rec, self.resolve)]
        self.assertNotIn("capsule-corp-worker:test", got)
        self.assertNotIn("capsule-web:fresh", got)

    def test_digest_in_record_keeps_image(self):
        self.rec.write_text(f"capsule-python@{D('1')}\n")
        got = [t for t, _ in image_sweep.plan(FakeDocker(), self.tmp, self.rec, self.resolve)]
        self.assertEqual(got, [DANG])

    def prev_plan(self, record=None, lock=None):
        """Plan over three keep-tagged images made by docker/build-base.sh: two old ones and a
        fresh one. `record` / `lock` name one of them."""
        f = FakeDocker()
        f.images = [(D("4"), "capsule-web", "prev-444444444444", None),
                    (D("5"), "capsule-web", "prev-555555555555", D("5")),
                    (D("6"), "capsule-web", "prev-666666666666", None)]

        def call(args, **kw):
            r = f(args, **kw)
            if args[:2] == ["image", "inspect"]:
                out = json.loads(r.stdout)
                out[2]["Created"] = NOW
                r.stdout = json.dumps(out)
            return r
        if record:
            self.rec.write_text(record)
        if lock:
            (self.tmp / "IMAGES.lock").write_text(lock)
        return [t for t, _ in image_sweep.plan(call, self.tmp, self.rec, self.resolve)]

    def test_unrecorded_old_prev_tags_are_removed(self):
        self.assertEqual(self.prev_plan(), ["capsule-web:prev-444444444444",
                                            "capsule-web:prev-555555555555"])

    def test_prev_tag_of_a_recorded_image_is_kept(self):
        for kw in ({"record": f"capsule-web@{D('5')}\n"}, {"lock": f"capsule-web@{D('5')}\n"}):
            self.assertEqual(self.prev_plan(**kw), ["capsule-web:prev-444444444444"])

    def test_project_prev_tags_follow_the_same_rule_but_other_project_tags_stay(self):
        f = FakeDocker()
        f.images = [(D("4"), "capsule-proj-web1", "prev-444444444444", None),
                    (D("5"), "capsule-proj-web1", "prev-555555555555", D("5")),
                    (D("6"), "capsule-proj-web1", "local", None)]
        self.rec.write_text(f"capsule-proj-web1@{D('5')}\n")
        got = [t for t, _ in image_sweep.plan(f, self.tmp, self.rec, self.resolve)]
        self.assertEqual(got, ["capsule-proj-web1:prev-444444444444"])

    def test_fresh_prev_tag_is_kept(self):
        self.assertNotIn("capsule-web:prev-666666666666", self.prev_plan())


if __name__ == "__main__":
    unittest.main()
