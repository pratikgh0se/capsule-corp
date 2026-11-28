"""disk_relief (D204): the 4-hourly fixed-rules relief. Everything runs against throwaway dirs: a
fake HOME, fake docker/npm/uv/brew/go/pip on PATH (shell scripts that record their argv), a fake
storage_gc.py, temp scratch and temp git repos for the worktrees. Never the real home, Docker,
caches or repos. Free space is a patched function so a test says exactly how low the disk is."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from disk_relief_fakes import FAKE_SLOW, FUTURE, Base, age, fresh  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disk_relief  # noqa: E402


class Gate(Base):
    def test_enough_free_space_removes_nothing(self):
        self.free(60)
        (self.scratch / "x.old").mkdir()
        age(self.scratch / "x.old", 3)
        rc, out = self.go()
        self.assertEqual(rc, 0)
        self.assertIn("enough free space", out)
        self.assertEqual((self.calls(), self.jsonl()), ([], []))
        self.assertTrue((self.scratch / "x.old").exists())

    def test_default_threshold_is_60_gb(self):
        del os.environ["OPS_DISK_RELIEF_FREE_GB"]
        self.free(59)
        self.assertIn("relieving", self.go()[1])
        self.free(61)
        self.assertIn("enough free space", self.go()[1])

    def test_under_threshold_runs_sections_in_order(self):
        rc, out = self.go()
        self.assertEqual(rc, 0)
        c = self.calls()
        self.assertEqual(c[0], "storage_gc --apply")
        for want in ("npm cache clean --force", "brew cleanup --prune=all -s",
                     "go clean -modcache", "go clean -cache", "pip cache purge"):
            self.assertIn(want, c)
        self.assertFalse(any(x.startswith(("docker", "uv")) for x in c), c)

    def test_stops_once_back_above_threshold(self):
        self.free(40, after_marker="storage_gc", then=70)
        rc, out = self.go()
        self.assertEqual(rc, 0)
        self.assertNotIn("npm cache clean --force", self.calls())
        self.assertIn("stopping", out)

    def test_hard_floor_alerts_only_when_still_under_it(self):
        self.free(20)
        rc, out = self.go()
        self.assertEqual(rc, 3)
        self.assertEqual(sum(l.startswith("disk-relief: ALERT") for l in out.splitlines()), 1)
        self.assertIn("20.0 GB", out)
        self.free(30)
        rc, out = self.go()
        self.assertEqual(rc, 0)
        self.assertNotIn("ALERT", out)


class DryRun(Base):
    def test_dry_run_changes_nothing(self):
        self.make_repo()
        old = self.add_wt("capsule-corp-old")
        (self.scratch / "x.old").mkdir()
        (self.scratch / "x.old" / "f").write_text("z")
        age(self.scratch / "x.old", 3)
        derived = self.home / "Library/Developer/Xcode/DerivedData/App-abc"
        derived.mkdir(parents=True)
        rc, out = self.go("--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(self.jsonl(), [])
        self.assertEqual(self.calls(), ["storage_gc "])
        self.assertTrue(old.exists() and derived.exists() and (self.scratch / "x.old").exists())
        for want in ("would remove claude-scratch", "would remove worktree",
                     "would remove cache npm", "would remove xcode-deriveddata"):
            self.assertIn(want, out)
        self.assertIn("[ancestor]", out)
        self.assertIn("(dry run)", out)


class Caches(Base):
    def _without_pip(self):
        """pip absent whatever else is on the machine: /usr/bin or /bin may hold a real one."""
        real = disk_relief.shutil.which

        def which(cmd, *a, **k):
            return None if cmd == "pip" else real(cmd, *a, **k)
        self.enterContext(mock.patch.object(disk_relief.shutil, "which", which))

    def test_present_tool_is_run(self):
        rc, out = self.go()
        self.assertEqual(rc, 0)
        self.assertIn("pip cache purge", self.calls())
        self.assertNotIn("pip cache skipped", out)

    def test_missing_tool_is_skipped_and_logged_as_skip(self):
        (self.bin / "pip").unlink()
        self._without_pip()
        rc, out = self.go()
        self.assertEqual(rc, 0)
        self.assertIn("pip cache skipped", out)
        self.assertNotIn("pip cache purge", self.calls())

    def test_docker_and_uv_are_never_touched(self):
        self.go()
        self.assertFalse(any(c.startswith(("docker", "uv")) for c in self.calls()))

    def test_relative_home_refuses_deriveddata_and_scratch(self):
        os.environ["HOME"] = "relative/home"
        with mock.patch.object(disk_relief.Path, "home", lambda: Path("relative/home")):
            rc, out = self.go()
        self.assertEqual(rc, 0)
        self.assertIn("DerivedData skipped: HOME is not an absolute path", out)
        self.assertIn("scratch skipped: HOME is not an absolute path", out)

    def test_a_tool_past_its_timeout_is_skipped_not_failed(self):
        self.fake("npm", FAKE_SLOW)
        with mock.patch.object(disk_relief, "TOOL_TIMEOUT", 1):
            rc, out = self.go()
        self.assertEqual(rc, 0)
        self.assertTrue(any(r["what"] == "cache" and r["result"] == "skipped: busy or in use"
                            for r in self.jsonl()))
        self.assertIn("pip cache purge", self.calls())      # the job went on

    def test_timeout_does_not_stop_the_floor_alert(self):
        self.free(20)
        self.fake("npm", FAKE_SLOW)
        with mock.patch.object(disk_relief, "TOOL_TIMEOUT", 1):
            rc, out = self.go()
        self.assertEqual(rc, 3)
        self.assertIn("ALERT", out)

    def test_deriveddata_contents_go_the_folder_stays(self):
        d = self.home / "Library/Developer/Xcode/DerivedData"
        (d / "App-abc").mkdir(parents=True)
        (d / "App-abc" / "x").write_text("1")
        (d / "loose").write_text("1")
        self.go()
        self.assertTrue(d.is_dir())
        self.assertEqual(list(d.iterdir()), [])

    def test_no_deriveddata_folder_is_fine(self):
        self.assertEqual(self.go()[0], 0)

    def test_failed_tool_is_logged_and_exit_1(self):
        self.fake("npm", '#!/bin/sh\nexit 2\n')
        rc, _ = self.go()
        self.assertEqual(rc, 1)
        self.assertTrue(any(r["what"] == "cache" and r["result"].startswith("FAILED")
                            for r in self.jsonl()))


class Scratch(Base):
    def test_selection_rules(self):
        s, other = self.scratch, self.tmp / "elsewhere"
        other.mkdir()
        (other / "precious").write_text("keep")
        for name in ("x.old", "tmp.old"):
            (s / name).mkdir()
            (s / name / "f").write_text("z")
            age(s / name, 3)
        (s / "tmp.fresh_inside").mkdir()
        (s / "tmp.fresh_inside" / "f").write_text("z")
        age(s / "tmp.fresh_inside", 3)
        fresh(s / "tmp.fresh_inside" / "f")                    # modified just now
        (s / "other.old").mkdir()
        age(s / "other.old", 3)                                 # name not x.* / tmp.*
        (s / "x.file").write_text("z")
        age(s / "x.file", 3)                                    # not a directory
        os.symlink(other, s / "x.link")                         # a link, never followed
        self.go()
        gone = {n for n in ("x.old", "tmp.old") if not (s / n).exists()}
        self.assertEqual(gone, {"x.old", "tmp.old"})
        for kept in ("tmp.fresh_inside", "other.old", "x.file", "x.link"):
            self.assertTrue(os.path.lexists(s / kept), kept)
        self.assertEqual((other / "precious").read_text(), "keep")
        removed = [r for r in self.jsonl() if r["what"] == "claude-scratch"]
        self.assertEqual({Path(r["id"]).name for r in removed}, {"x.old", "tmp.old"})
        self.assertTrue(all(r["result"] == "removed" for r in removed))

    def test_a_link_as_the_root_is_refused(self):
        real = self.tmp / "real"
        real.mkdir()
        (real / "x.old").mkdir()
        age(real / "x.old", 3)
        link = self.tmp / "link-root"
        os.symlink(real, link)
        with mock.patch.object(disk_relief, "SCRATCH_ROOT", str(link)):
            _, out = self.go()
        self.assertIn("scratch skipped", out)
        self.assertTrue((real / "x.old").exists())


class Worktrees(Base):
    def setUp(self):
        super().setUp()
        self.make_repo()

    def removed(self):
        return {Path(r["id"]).name for r in self.jsonl() if r["what"] == "worktree"
                and r["result"].startswith("removed")}

    def test_clean_merged_old_worktree_goes_and_branches_stay(self):
        old = self.add_wt("capsule-corp-old")
        self.git(self.repo, "branch", "build/keepme")
        rc, _ = self.go()
        self.assertEqual(rc, 0)
        self.assertFalse(old.exists())
        self.assertEqual(self.removed(), {"capsule-corp-old"})
        self.assertIn("build/keepme", self.git(self.repo, "branch"))
        self.assertTrue(self.repo.exists())

    def test_uncommitted_work_is_kept(self):
        dirty = self.add_wt("capsule-corp-dirty")
        (dirty / "new.txt").write_text("work")
        age(dirty, 3)
        self.go()
        self.assertTrue((dirty / "new.txt").exists())
        self.assertEqual(self.removed(), set())

    def test_modified_tracked_file_is_kept(self):
        dirty = self.add_wt("capsule-corp-edit")
        (dirty / "f").write_text("changed")
        age(dirty, 3)
        self.go()
        self.assertEqual((dirty / "f").read_text(), "changed")

    def test_head_not_on_main_is_kept(self):
        off = self.add_wt("capsule-corp-off")
        (off / "g").write_text("2")
        self.git(off, "add", "g")
        self.git(off, "commit", "-q", "-m", "off main")
        age(off, 3)
        self.go()
        self.assertTrue(off.exists())
        self.assertEqual(self.removed(), set())

    def test_protected_and_foreign_folders_are_kept(self):
        keep = [self.add_wt(n) for n in ("capsule-corp-keeper-1", "capsule-corp-tests",
                                         "other-name")]
        self.go()
        self.assertTrue(all(p.exists() for p in keep))
        self.assertEqual(self.removed(), set())

    def test_locked_worktree_is_kept(self):
        locked = self.add_wt("capsule-corp-locked")
        self.git(self.repo, "worktree", "lock", str(locked))
        self.go()
        self.assertTrue(locked.exists())

    def test_recently_modified_worktree_is_kept(self):
        fresh = self.add_wt("capsule-corp-fresh", old=False)
        self.go()
        self.assertTrue(fresh.exists())

    def test_without_a_reachable_origin_nothing_is_removed(self):
        old = self.add_wt("capsule-corp-old")
        self.git(self.repo, "remote", "set-url", "origin", str(self.tmp / "nowhere.git"))
        _, out = self.go()
        self.assertTrue(old.exists())
        self.assertIn("worktrees skipped", out)

    def test_no_force_flag_is_ever_passed_to_git(self):
        src = (Path(disk_relief.__file__)).read_text()
        self.assertNotIn("--force", src.split("def _no_reason")[0].split("class _Relief")[1])


if __name__ == "__main__":
    unittest.main()
