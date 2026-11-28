"""R11: nightly image GC (maintenance/image_gc.py) against a fake `docker` on PATH.

Oracle 1: the fake's image store after the run, read back by the test. Oracle 2: the lock file's
digests, its git history (a throwaway repository per test) and a hand-written list of what must
remain, not the GC's own verdicts. Z1: the web_stack intent rows replay in test_web_stack.py.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_stack_fakes import N_IMAGES, ROOT, Fake, digest, image, scenario, script  # noqa: E402


class Gc(Fake):
    def git(self, *args):
        ident = ("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false")
        subprocess.run(["git", "-C", str(self.tmp), *ident, *args], check=True,
                       capture_output=True)

    def commit_lock(self, *rows, note="comment"):
        """Write the lock from (name, digest char) rows and commit it as the newest version."""
        self.lock.write_text(f"# {note}\n" + "".join(f"{n}@{digest(c)}\n" for n, c in rows))
        self.git("add", "IMAGES.lock")
        self.git("commit", "-q", "-m", "lock")

    def current(self, base="a", python="1"):
        """The rows of the newest lock, with base and python as given."""
        return (("capsule-base", base), ("capsule-python", python), ("capsule-web", "7"))

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.commit_lock(("capsule-base", "c"))                  # two versions back: not kept
        self.commit_lock(("capsule-base", "e"), ("capsule-python", "2"))   # the previous lock
        self.commit_lock(("capsule-base", "a"), ("capsule-python", "1"), ("capsule-web", "7"))
        self.store(scenario(), boxes=[digest("d")])

    def rm_calls(self):
        return [c for c in self.calls() if c.startswith("image rm")]

    def test_apply_keeps_lock_previous_in_use_and_recent_and_removes_the_rest(self):
        r = self.gc("1")
        self.assertEqual(r.returncode, 0, r.stderr)
        # hand-written: lock a,1,5 (5 by RepoDigests); previous lock e (base), 2 (python);
        # recent f; in use d; the owner's own tag m; two tags n; unlabelled u. Gone: b c 3 6.
        self.assertEqual(self.left(), set("aefdm12n5u"))
        self.assertIn("keep " + "d" * 12 + " <untagged> (in use)", r.stdout)
        self.assertNotIn(digest("d"), " ".join(self.rm_calls()))

    def test_every_lock_digest_survives_apply(self):
        self.gc("1")
        wanted = {line.split("@sha256:")[1][0] for line in self.lock.read_text().splitlines()
                  if "@sha256:" in line}
        self.assertEqual(wanted, {"a", "1", "7"})
        self.assertTrue({"a", "1"} <= self.left())
        self.assertIn("5", self.left())    # its lock digest is its RepoDigests entry
        self.assertNotIn("6", self.left())  # the newer web image is in no lock

    def test_previous_comes_from_the_lock_history_in_git_not_from_a_guess(self):
        r = self.gc("1")
        self.assertIn("keep " + "e" * 12 + " <untagged> (previous)", r.stdout)
        self.assertIn("keep " + "2" * 12 + " <untagged> (previous)", r.stdout)
        self.assertNotIn("c", self.left())     # only in the lock two versions back

    def test_a_rebuilt_but_uncommitted_lock_keeps_the_newest_committed_digest_as_previous(self):
        self.lock.write_text("".join(f"{n}@{digest(c)}\n" for n, c in self.current("b")))
        self.gc("1")
        # base: b is the lock's, a the newest committed; python and web are unchanged (python's
        # previous is 2, from the version before); c, e, 3 and 6 are in no kept version
        self.assertEqual(self.left(), set("abf1" + "2dmn5u"))

    def test_a_testers_own_build_in_the_image_record_is_kept(self):
        home = self.tmp / "capsule-home"
        home.mkdir()
        rows = f"capsule-base@{digest('b')}\ncapsule-python@{digest('6')}\n"
        (home / "images.lock").write_text(rows)
        self.gc("1")
        self.assertTrue(set("b6") <= self.left(), self.left())     # old and unlabelled by the lock
        self.assertFalse(set("c3") & self.left(), self.left())     # still gone: in no record

    def test_a_layer_deleted_only_in_the_working_tree_keeps_its_committed_digest(self):
        self.store(scenario() + [image("8", "base", 90)], boxes=[digest("d")])
        self.commit_lock(("capsule-base", "8"), ("capsule-python", "1"), ("capsule-web", "7"))
        self.lock.write_text("".join(f"{n}@{digest(c)}\n" for n, c in self.current()[1:]))
        self.gc("1")
        self.assertIn("8", self.left())       # the committed base digest, base row gone from disk
        self.assertFalse(set("ce") & self.left(), self.left())

    def test_a_lock_row_with_a_hyphen_in_the_name_keeps_its_image(self):
        self.store(scenario() + [image("4", "proj-x", 90)], boxes=[digest("d")])
        self.commit_lock(*self.current(), ("capsule-proj-x", "4"))
        r = self.gc("1")
        self.assertIn("4", self.left())
        self.assertIn("keep " + "4" * 12 + " <untagged> (lock)", r.stdout)

    def test_a_comment_only_commit_does_not_push_a_previous_digest_out(self):
        self.commit_lock(*self.current("b"))                       # base rebuilt: a is previous
        self.commit_lock(*self.current("b"), note="only a comment")
        self.gc("1")
        self.assertTrue(set("ab12") <= self.left(), self.left())    # a: previous base
        self.assertFalse(set("ce36") & self.left(), self.left())

    def test_a_rebuild_of_only_python_keeps_the_previous_of_base_and_of_python(self):
        self.commit_lock(*self.current("b"))                       # base rebuilt: a is previous
        self.commit_lock(*self.current("b", "3"))                  # then only python rebuilt
        self.gc("1")
        self.assertTrue(set("ab13") <= self.left(), self.left())    # a base prev, 1 python prev
        self.assertFalse(set("ce26") & self.left(), self.left())

    def test_the_history_walk_stops_at_the_bound(self):
        for n in range(3):
            self.commit_lock(*self.current(), note=f"comment {n}")
        sys.path.insert(0, str(ROOT / "maintenance"))
        import image_gc
        self.assertEqual(image_gc._previous_digests(self.lock), {digest("e"), digest("2")})
        with mock.patch.object(image_gc, "HISTORY", 3):    # e and 2 are four commits back
            self.assertEqual(image_gc._previous_digests(self.lock), set())

    def test_if_the_lock_history_cannot_be_read_nothing_is_removed(self):
        text = self.lock.read_text()
        for init in (False, True):       # no repository at all; a repository, lock never committed
            where = Path(tempfile.mkdtemp(prefix="web-stack-lock-"))
            self.addCleanup(shutil.rmtree, where, True)
            if init:
                subprocess.run(["git", "-C", str(where), "init", "-q"], check=True)
            (where / "IMAGES.lock").write_text(text)
            self.lock = where / "IMAGES.lock"
            r = self.gc("1")
            self.assertEqual((r.returncode, len(self.left())), (0, N_IMAGES), init)
            self.assertIn("nothing removed", r.stderr)
            self.assertEqual(self.rm_calls(), [])

    def test_an_image_with_a_tag_outside_capsule_is_kept(self):
        r = self.gc("1")
        self.assertIn("m", self.left())
        self.assertIn("keep " + "m" * 12 + " myapp:dev (tag outside capsule-*)", r.stdout)

    def test_removal_is_by_image_id_and_an_image_with_several_tags_is_kept(self):
        r = self.gc("1")
        self.assertIn("n", self.left())
        self.assertIn("keep " + "n" * 12 + " capsule-python:x1 capsule-python:x2 (2 tags)",
                      r.stdout)
        self.assertEqual(sorted(self.rm_calls()),
                         sorted(f"image rm {digest(c)}" for c in "bc36"))
        self.assertNotIn("capsule-base:old", " ".join(self.rm_calls()))

    def test_dry_run_is_the_default_and_removes_nothing(self):
        before = self.left()
        r = self.gc()
        self.assertEqual((r.returncode, self.left()), (0, before))
        self.assertEqual(self.rm_calls(), [])
        for gone in "bc36":
            self.assertIn("would remove " + gone * 12, r.stdout)
        self.assertIn("fake-df-report", r.stdout)

    def test_only_the_exact_value_1_applies(self):
        for value in ("0", "", "true", "yes", "on", "2", " 1 ", "1 ", " 1", "01", "1\n"):
            self.gc(value)
            self.assertEqual(len(self.left()), N_IMAGES, repr(value))
        self.gc("1")
        self.assertEqual(len(self.left()), N_IMAGES - 4)

    def test_unlabelled_images_are_never_listed_for_removal_or_removed(self):
        r = self.gc("1")
        self.assertIn("u", self.left())
        self.assertNotIn("redis", r.stdout)
        self.assertFalse([c for c in self.calls() if "redis" in c])

    def test_only_listing_and_image_rm_calls_are_ever_made(self):
        self.gc("1")
        allowed = ("image ls", "image inspect", "ps ", "inspect ", "image rm ", "system df")
        for c in self.calls():
            self.assertTrue(c.startswith(allowed), c)
            self.assertNotIn("prune", c)
            self.assertNotIn("volume", c)
            self.assertNotIn(" -f", c)

    def test_an_unreadable_or_empty_lock_removes_nothing_and_exits_0(self):
        self.lock.write_text("# no rows\n")
        r = self.gc("1")
        self.assertEqual((r.returncode, len(self.left())), (0, N_IMAGES))
        self.lock.unlink()
        r = self.gc("1")
        self.assertEqual((r.returncode, len(self.left())), (0, N_IMAGES))

    def test_docker_failing_or_missing_removes_nothing_and_exits_0(self):
        script(self.bin / "docker", "#!/bin/sh\nexit 1\n")
        r = self.gc("1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing removed", r.stderr)
        (self.bin / "docker").unlink()
        r = self.gc("1")
        self.assertEqual((r.returncode, len(self.left())), (0, N_IMAGES))

    def test_an_image_with_an_unreadable_build_time_is_kept(self):
        odd = image("9", "base", 300)
        odd["Created"] = "not a time"
        self.store(scenario() + [odd], boxes=[digest("d")])
        self.gc("1")
        self.assertIn("9", self.left())

    def test_the_wiring_in_run_sh_defaults_the_switch_off(self):
        text = (ROOT / "maintenance" / "run.sh").read_text()
        self.assertIn('OPS_IMAGE_GC_APPLY="${OPS_IMAGE_GC_APPLY:-0}"', text)
        self.assertIn('"$MAINT_DIR/image_gc.py"', text)


if __name__ == "__main__":
    import unittest
    unittest.main()
