# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   TempDirs.test_the_real_guard_accepts_only_macos_temp_roots
"""storage_gc's temp-dir section and its Z1 intent rows (row 643), on throwaway temp dirs only.
The unsafe-root test is preview-only on purpose: it must stay harmless even if the guard breaks.
Fakes: storage_gc_fakes.py."""
import os
import shutil
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from storage_gc_fakes import (H64, REAL_TMP_ROOTS, Base, FakeDocker, box,  # noqa: E402
                              storage_gc, vol)
from test_storage_gc_ages import Replays  # noqa: E402

OLD = (time.time() - 172800,) * 2


class TempDirs(Base):
    def test_apply_removes_only_old_untouched_suite_dirs(self):
        gone = [self.mk("tmpAbCd1234"), self.mk("tmp.AbCdEfGhIj"), self.mk("capsule-suite-x"),
                self.mk("mutation-check-1"), self.mk("lander-box-9")]
        live = self.mk("tmpZZZZ9999", inner_hours=1)                   # file modified inside
        young = self.mk("capsule-suite-new", hours=2)
        foreign = self.mk("other-old-dir")                             # not a suite name
        odd = self.mk("tmpshort")                                      # 5 chars after tmp: no
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("precious")
        (self.tmpdir / "lander-box-link").symlink_to(outside, target_is_directory=True)
        rc, out, _ = self.gc(FakeDocker(), "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual([d.exists() for d in gone], [False] * 5)
        self.assertTrue(all(d.exists() for d in (live, young, foreign, odd)))
        self.assertEqual((outside / "keep.txt").read_text(), "precious")
        self.assertTrue((self.tmpdir / "lander-box-link").is_symlink())
        self.assertIn("kept: modified inside within 1 day: 1 tmpdirs", out)
        self.assertIn("kept: not a real directory (links are never followed): 1 tmpdirs", out)
        self.assertEqual(len([r for r in self.log() if r["what"] == "tmpdir"]), 5)

    def test_read_only_nested_dirs_do_not_stop_the_removal(self):
        d = self.mk("capsule-suite-ro")
        sub = d / "sub"
        sub.mkdir()
        (sub / "f").write_text("y")
        for p in (sub / "f", sub, d):
            os.utime(p, OLD)
        sub.chmod(0o500)
        self.gc(FakeDocker(), "--apply")
        self.assertFalse(d.exists())

    def test_chmod_on_a_mode_000_dir(self):
        # end to end such a tree is kept as unreadable before it gets here, so test the step:
        # lchmod (macOS) opens it up; the fd fallback cannot open it, raises and changes nothing
        locked = self.mk("capsule-suite-locked")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        if os.chmod in os.supports_follow_symlinks:
            self.assertTrue(storage_gc._chmod_dir(str(locked)))
            self.assertEqual(locked.stat().st_mode & 0o777, 0o700)
        else:
            self.assertRaises(PermissionError, storage_gc._chmod_dir, str(locked))
            self.assertEqual(locked.stat().st_mode & 0o777, 0)

    def test_preview_deletes_nothing(self):
        d = self.mk("capsule-suite-x")
        _, out, _ = self.gc(FakeDocker())
        self.assertTrue(d.exists())
        self.assertIn("would remove tmpdir " + str(d), out)
        self.assertIn("storage-gc: tmpdir: 1 chosen", out)

    def test_another_owners_dir_is_kept(self):
        d = self.mk("capsule-suite-x")
        keep, _ = storage_gc._tmp_verdict(str(d), time.time(), os.getuid() + 1, os.lstat(d).st_dev)
        self.assertEqual(keep, "other owner")

    def test_a_dir_on_another_device_is_kept(self):
        d = self.mk("capsule-suite-x")
        keep, _ = storage_gc._tmp_verdict(str(d), time.time(), os.getuid(), os.lstat(d).st_dev + 1)
        self.assertEqual(keep, "different device")

    def test_a_mount_point_inside_keeps_the_dir_and_the_walk_never_removes_it(self):
        d = self.mk("capsule-suite-x")
        (d / "mnt").mkdir()
        (d / "mnt" / "f").write_text("on another disk")
        for p in (d / "mnt" / "f", d / "mnt", d):
            os.utime(p, OLD)
        real = os.lstat

        def lstat(p, *a, **kw):
            s = real(p, *a, **kw)
            dev = s.st_dev + 1 if str(p).endswith("/mnt") else s.st_dev
            return types.SimpleNamespace(st_dev=dev, st_mtime=s.st_mtime, st_blocks=s.st_blocks,
                                         st_mode=s.st_mode, st_uid=s.st_uid)
        with mock.patch.object(storage_gc.os, "lstat", lstat):
            keep, _ = storage_gc._tmp_verdict(str(d), time.time(), os.getuid(), real(d).st_dev)
            self.assertEqual(keep, "mount point inside (different device)")
            self.assertTrue(storage_gc._rm_tmp({"id": str(d)}, 0).startswith("skipped: mount"))
        self.assertTrue((d / "mnt" / "f").exists())

    def test_chmod_never_goes_through_a_link(self):
        target = self.tmp / "target"
        target.mkdir()
        target.chmod(0o755)
        link = self.tmpdir / "capsule-suite-link"
        link.symlink_to(target, target_is_directory=True)
        self.assertFalse(storage_gc._chmod_dir(str(link)))
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        real = self.mk("capsule-suite-real")
        real.chmod(0o500)
        self.assertTrue(storage_gc._chmod_dir(str(real)))
        self.assertEqual(real.stat().st_mode & 0o777, 0o700)

    def test_a_dir_swapped_for_a_link_before_its_removal_is_left_alone(self):
        d = self.mk("capsule-suite-x")
        target = self.tmp / "target"
        target.mkdir()
        (target / "keep").write_text("precious")
        shutil.rmtree(d)
        d.symlink_to(target, target_is_directory=True)
        self.assertTrue(storage_gc._rm_tmp({"id": str(d)}, 0)
                        .startswith("skipped: not a real directory"))
        self.assertEqual((target / "keep").read_text(), "precious")

    def test_a_dir_that_vanishes_while_listing_does_not_fail_the_section(self):
        d = self.mk("capsule-suite-x")
        keeper = self.mk("capsule-suite-y")
        real = storage_gc._tmp_verdict

        def verdict(path, *a):
            if path == str(d):
                shutil.rmtree(d)
            return real(path, *a)
        with mock.patch.object(storage_gc, "_tmp_verdict", verdict):
            rc, out, _ = self.gc(FakeDocker())
        self.assertEqual(rc, 0)
        self.assertIn("storage-gc: tmpdir: 1 chosen", out)
        self.assertTrue(keeper.exists())

    def test_a_dir_that_vanished_before_its_removal_is_skipped_gone(self):
        d = self.mk("capsule-suite-x")
        real = storage_gc._rm_tmp

        def late(item, now):
            shutil.rmtree(d)
            return real(item, now)
        with mock.patch.object(storage_gc, "_rm_tmp", late):
            rc, _, _ = self.gc(FakeDocker(), "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual([r["result"] for r in self.log() if r["what"] == "tmpdir"],
                         ["skipped: gone"])

    def test_a_half_removed_dir_fails_instead_of_claiming_gone(self):
        d = self.mk("capsule-suite-x")
        (d / "sub").mkdir()
        for p in (d / "sub", d):
            os.utime(p, OLD)
        real = storage_gc._chmod_dir

        def chmod(path):
            if path.endswith("/sub"):
                raise FileNotFoundError(path)       # something inside vanished mid-walk
            return real(path)
        with mock.patch.object(storage_gc, "_chmod_dir", chmod):
            rc, _, err = self.gc(FakeDocker(), "--apply")
        self.assertEqual(rc, 1)
        self.assertEqual([r["result"] for r in self.log() if r["what"] == "tmpdir"],
                         ["FAILED: partly removed"])
        self.assertTrue(d.exists())
        self.assertIn("an item failed", err)

    def test_a_dir_that_became_busy_before_its_removal_is_skipped(self):
        d = self.mk("capsule-suite-x")
        (d / "fresh").write_text("now")
        self.assertTrue(storage_gc._rm_tmp({"id": str(d)}, time.time()).startswith("skipped"))
        self.assertTrue(d.exists())

    def test_the_real_guard_accepts_only_macos_temp_roots(self):
        self.assertEqual(REAL_TMP_ROOTS, ("/private/var/folders/", "/private/tmp/"))
        with mock.patch.object(storage_gc, "TMP_ROOTS", REAL_TMP_ROOTS):
            for raw in ("/usr/lib", "/usr/bin"):    # real dirs in no macOS temp root; preview only
                with self.subTest(raw=raw), mock.patch.dict(os.environ, {"CLEANUP_TMPDIR": raw}):
                    rc, out, err = self.gc(FakeDocker())
                    self.assertEqual(rc, 1)
                    self.assertIn("not a macOS temp root", err)
                    self.assertNotIn("storage-gc: tmpdir:", out)

    def test_the_temp_section_refuses_unsafe_roots_but_docker_sections_still_run(self):
        victim = self.tmp / "capsule-suite-old"        # in a parent of $HOME: must survive
        victim.mkdir()
        os.utime(victim, OLD)
        (self.tmp / "homelink").symlink_to(self.home, target_is_directory=True)
        bad = ("//", "/.", ".", str(self.home), str(self.tmp / "homelink"), str(self.tmp),
               "/usr/lib", str(self.tmp / "nope"))
        for raw in bad:     # preview only: harmless even if the guard breaks
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"CLEANUP_TMPDIR": raw}):
                rc, out, err = self.gc(FakeDocker())
                self.assertEqual(rc, 1)
                self.assertIn("FAILED tmpdir: temp dir", err)
                self.assertIn("storage-gc: container: 0 chosen", out)
                self.assertNotIn("storage-gc: tmpdir:", out)    # the section never listed anything
                self.assertTrue(victim.exists())


class IntentRows(Replays, Base):
    """Z1 (row 643): contracts/intent/storage_gc.jsonl, replayed once the CoS has stamped it, the
    same pattern as test_web_stack.Rows. The built-in cases run with or without the file. The age
    rows (labelled leftovers, legacy sweep, kind-image container) replay from the Replays mixin in
    test_storage_gc_ages."""
    CASES = {"running-container-kept": "running", "volumes-not-proven-kept": "volumes",
             "symlink-named-like-a-suite-dir-kept": "symlink",
             "labelled-test-and-run-leftovers-go-after-a-day": "labelled",
             "pre-label-leftovers-swept-once": "legacy",
             "stopped-kind-image-container-kept": "container"}

    def replay(self, case):
        age = {"labelled": self.replay_labelled, "legacy": self.replay_legacy,
               "container": self.replay_container}
        if case in age:
            return age[case]()
        if case == "running":
            fake = FakeDocker([box("1", "w-old"), box("2", "w-running", status="running"),
                               box("3", "w-young", hours=0)])
            self.gc(fake, "--apply")
            left = {b["Id"][0] for b in fake.boxes}
            return {"exit": 0, "running_removed": int("2" not in left),
                    "young_removed": int("3" not in left), "stopped_removed": int("1" not in left)}
        if case == "volumes":
            fake = FakeDocker([box("a", "w-stopped", hours=0, volumes=["m" * 64])],
                              [vol("m" * 64, labels={"capsule": "1"}), vol(H64, days=0.5),
                               vol("f" * 64, labels={"owner": "me"}, days=9),
                               vol("capsule-corp_pg", labels={"capsule": "1"}),
                               vol("scratch", labels={"capsule": "1"})])
            self.gc(fake, "--apply")
            left = {v["Name"] for v in fake.vols}
            return {"exit": 0, "mounted_removed": int("m" * 64 not in left),
                    "unlabelled_anonymous_removed": int(H64 not in left),
                    "labelled_non_capsule_removed": int("f" * 64 not in left),
                    "compose_removed": int("capsule-corp_pg" not in left),
                    "labelled_unused_removed": int("scratch" not in left)}
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("precious")
        link = self.tmpdir / "lander-box-link"
        link.symlink_to(outside, target_is_directory=True)
        old = self.mk("capsule-suite-old")
        self.gc(FakeDocker(), "--apply")
        return {"exit": 0, "link_removed": int(not link.is_symlink()),
                "target_files_lost": int(not (outside / "keep.txt").exists()),
                "real_old_dir_removed": int(not old.exists())}

    def test_the_built_in_cases_hold_without_the_file(self):
        self.assertEqual(self.replay("running"),
                         {"exit": 0, "running_removed": 0, "young_removed": 0,
                          "stopped_removed": 1})
        self.assertEqual(self.replay("volumes"),
                         {"exit": 0, "mounted_removed": 0, "unlabelled_anonymous_removed": 0,
                          "labelled_non_capsule_removed": 0, "compose_removed": 0,
                          "labelled_unused_removed": 1})
        self.assertEqual(self.replay("labelled"),
                         {"exit": 0, "old_unused_test_or_run_removed": 2, "young_removed": 0,
                          "kind_image_removed": 0, "no_kind_removed": 0, "in_use_removed": 0,
                          "locked_removed": 0, "compose_removed": 0})
        self.assertEqual(self.replay("legacy"),
                         {"exit": 0, "old_legacy_removed": 3, "after_cut_over_removed": 0,
                          "young_removed": 0, "named_or_labelled_or_in_use_removed": 0,
                          "moved_tag_removed": 0, "real_worker_image_removed": 0})
        self.assertEqual(self.replay("container"),
                         {"exit": 0, "kind_image_removed": 0, "no_kind_container_removed": 1})
        self.assertEqual(self.replay("symlink"),
                         {"exit": 0, "link_removed": 0, "target_files_lost": 0,
                          "real_old_dir_removed": 1})

    def test_the_intent_rows_hold_when_the_file_exists(self):
        from capsule import intent
        rows = intent.load("storage_gc")
        self.assertEqual({r["id"] for r in rows}, set(self.CASES))
        for row in rows:
            got = self.replay(self.CASES[row["id"]])
            for key, want in row["expect"].items():
                if isinstance(want, int):
                    self.assertEqual(got[key], want, row["id"])


if __name__ == "__main__":
    import unittest
    unittest.main()
