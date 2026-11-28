"""disk_relief (D204), second half: the worktree rules added in fix round 1 and the job-level
guards (deadline, section errors, fetch hardening). Same throwaway setup as test_disk_relief."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from disk_relief_fakes import Base  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disk_relief  # noqa: E402


class Worktrees2(Base):
    def setUp(self):
        super().setUp()
        self.make_repo()

    def reasons(self):
        return {Path(r["id"]).name: r["result"] for r in self.jsonl() if r["what"] == "worktree"}

    def ignore_and_catch_up(self, wt, patterns):
        (self.repo / ".gitignore").write_text(patterns)
        self.git(self.repo, "add", ".gitignore")
        self.git(self.repo, "commit", "-q", "-m", "ignore")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(wt, "merge", "-q", "--ff-only", "main")

    def test_ancestor_reason_is_logged(self):
        self.add_wt("capsule-corp-old")
        self.go()
        self.assertEqual(self.reasons(), {"capsule-corp-old": "removed: ancestor"})

    def test_ignored_env_file_keeps_the_worktree(self):
        wt = self.add_wt("capsule-corp-env")
        self.ignore_and_catch_up(wt, ".env\n__pycache__/\n")
        (wt / ".env").write_text("SECRET=1")
        (wt / "__pycache__").mkdir()
        (wt / "__pycache__" / "m.pyc").write_text("x")
        self.go()
        self.assertTrue((wt / ".env").exists())
        self.assertEqual(self.reasons(), {})

    def test_only_allowlisted_ignored_folders_do_not_keep_it(self):
        wt = self.add_wt("capsule-corp-cache")
        self.ignore_and_catch_up(wt, "__pycache__/\nnode_modules/\n")
        (wt / "pkg" / "__pycache__").mkdir(parents=True)
        (wt / "pkg" / "__pycache__" / "m.pyc").write_text("x")
        (wt / "node_modules").mkdir()
        (wt / "node_modules" / "a").write_text("x")
        self.go()
        self.assertFalse(wt.exists())

    def ignored_wt(self, name, patterns, files):
        wt = self.add_wt(name)
        self.ignore_and_catch_up(wt, patterns)
        for rel in files:
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text("x")
        self.go()
        return wt

    def test_only_ignored_logs_do_not_keep_it(self):
        wt = self.ignored_wt("capsule-corp-logs", "maintenance/logs/\n*.log\n",
                             ["maintenance/logs/x.log", "run.log"])
        self.assertFalse(wt.exists())

    def test_logs_plus_another_ignored_file_keeps_it(self):
        wt = self.ignored_wt("capsule-corp-logenv", "maintenance/logs/\n.env\n",
                             ["maintenance/logs/x.log", ".env"])
        self.assertTrue((wt / ".env").exists())

    def test_log_lookalikes_keep_it(self):
        for name, ignored, rel in (("capsule-corp-cat", "catalog.txt", "catalog.txt"),
                                   ("capsule-corp-blogs", "blogs/", "blogs/a"),
                                   ("capsule-corp-logfile", "logs", "logs"),
                                   ("capsule-corp-dotlog", "x.log.bak", "x.log.bak")):
            with self.subTest(name):
                self.assertTrue(self.ignored_wt(name, ignored + "\n", [rel]).exists())

    def equivalent(self, name, n, detached, picked=None):
        """A worktree with n commits, each (or the first `picked`) cherry-picked onto main, which
        then moves on so the trees differ."""
        wt = self.add_wt(name)
        if not detached:
            self.git(wt, "checkout", "-q", "-b", f"br-{name}")
        shas = []
        for i in range(n):
            (wt / f"g{i}").write_text(str(i))
            self.git(wt, "add", f"g{i}")
            self.git(wt, "commit", "-q", "-m", f"c{i}")
            shas.append(self.git(wt, "rev-parse", "HEAD"))
        with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": "2031-01-01T00:00:00"}):
            for sha in shas[:picked if picked is not None else n]:      # new commit ids
                self.git(self.repo, "cherry-pick", sha)
        (self.repo / "later").write_text("x")
        self.git(self.repo, "add", "later")
        self.git(self.repo, "commit", "-q", "-m", "later")
        self.git(self.repo, "push", "-q", "origin", "main")
        return wt

    def test_patch_equivalent_named_branch_is_removed(self):
        wt = self.equivalent("capsule-corp-pe", 2, detached=False)
        self.go()
        self.assertFalse(wt.exists())
        self.assertEqual(self.reasons(), {"capsule-corp-pe": "removed: patch-equivalent"})

    def test_patch_equivalent_detached_head_is_kept(self):
        wt = self.equivalent("capsule-corp-det", 2, detached=True)
        self.go()
        self.assertTrue(wt.exists())

    def test_one_unmerged_patch_keeps_it(self):
        wt = self.equivalent("capsule-corp-plus", 2, detached=False, picked=1)
        self.go()
        self.assertTrue(wt.exists())

    def test_more_than_fifty_commits_falls_to_the_content_rule(self):
        """Past the cherry cap the patch rule gives up; the files all being on main still counts."""
        wt = self.equivalent("capsule-corp-big", 51, detached=False)
        self.go()
        self.assertFalse(wt.exists())
        self.assertEqual(self.reasons(), {"capsule-corp-big": "removed: content on main"})

    def test_same_tree_is_removed(self):
        wt = self.add_wt("capsule-corp-tree")
        (wt / "g").write_text("2")
        self.git(wt, "add", "g")
        self.git(wt, "commit", "-q", "-m", "wt side")
        (self.repo / "g").write_text("2")
        self.git(self.repo, "add", "g")
        self.git(self.repo, "commit", "-q", "-m", "main side")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.go()
        self.assertEqual(self.reasons(), {"capsule-corp-tree": "removed: same tree"})

    def test_failed_fetch_is_a_failure_and_skips_the_worktrees(self):
        old = self.add_wt("capsule-corp-old")
        self.git(self.repo, "remote", "set-url", "origin", str(self.tmp / "nowhere.git"))
        rc, out = self.go()
        self.assertEqual(rc, 1)
        self.assertTrue(old.exists())

    def test_a_vanished_file_skips_that_worktree_only(self):
        a, b = self.add_wt("capsule-corp-a"), self.add_wt("capsule-corp-b")
        real = disk_relief.scan

        def flaky(path, seconds):
            if str(path).endswith("capsule-corp-a"):
                raise FileNotFoundError(path)
            return real(path, seconds)
        with mock.patch.object(disk_relief, "scan", flaky):
            self.go()
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())

    def test_malformed_worktree_block_is_skipped(self):
        old = self.add_wt("capsule-corp-old")
        real = disk_relief.run

        def odd(cmd, *a, **k):
            r = real(cmd, *a, **k)
            if "worktree" in cmd and "list" in cmd:
                r.stdout += "\0HEAD abc\0\0"
            return r
        with mock.patch.object(disk_relief, "run", odd):
            rc, out = self.go()
        self.assertEqual(rc, 0)
        self.assertIn("malformed", out)
        self.assertFalse(old.exists())

    def test_deadline_skips_sections_but_the_floor_alert_fires(self):
        self.free(20)
        os.environ["OPS_DISK_RELIEF_DEADLINE_S"] = "0"
        self.addCleanup(os.environ.pop, "OPS_DISK_RELIEF_DEADLINE_S", None)
        rc, out = self.go()
        self.assertEqual(rc, 3)
        self.assertEqual(self.calls(), [])
        self.assertTrue(any(r["what"] == "deadline" for r in self.jsonl()))

    def test_a_section_error_does_not_skip_the_floor_alert(self):
        self.free(20)

        def _caches(_self):
            raise KeyError("boom")
        with mock.patch.object(disk_relief._Relief, "_caches", _caches):
            rc, out = self.go()
        self.assertEqual(rc, 3)

    # -- squash landings: the branch is not an ancestor, but its content is on main -------------
    def squash_wt(self, name, main_text="new", gone=False):
        """A branch worktree adds `n`, edits `f`, optionally deletes `g`; main gets the same change
        as one new commit (a squash), with `n` holding `main_text`."""
        (self.repo / "g").write_text("g")
        self.git(self.repo, "add", "g")
        self.git(self.repo, "commit", "-q", "-m", "g")
        self.git(self.repo, "push", "-q", "origin", "main")
        wt = self.tmp / name
        self.git(self.repo, "worktree", "add", "-q", "-b", name, str(wt), "main")
        (wt / "n").write_text("new")
        (wt / "f").write_text("2")
        if gone:
            (wt / "g").unlink()
        self.git(wt, "add", "-A")
        self.git(wt, "commit", "-q", "-m", "work")
        (self.repo / "n").write_text(main_text)
        (self.repo / "f").write_text("2")
        (self.repo / "other").write_text("later work")     # main moved on: the trees differ
        if gone:
            (self.repo / "g").unlink()
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "squash")
        self.git(self.repo, "push", "-q", "origin", "main")
        return wt

    def test_squash_landed_clean_worktree_is_removed(self):
        wt = self.squash_wt("capsule-corp-sq")
        self.go()
        self.assertFalse(wt.exists())
        self.assertEqual(self.reasons(), {"capsule-corp-sq": "removed: content on main"})
        self.assertTrue(self.git(self.repo, "rev-parse", "--verify", "capsule-corp-sq"))

    def test_squash_with_one_file_differing_on_main_is_kept(self):
        wt = self.squash_wt("capsule-corp-diff", main_text="other")
        self.go()
        self.assertTrue(wt.exists())
        self.assertEqual(self.reasons(), {})

    def test_squash_landed_but_dirty_is_kept(self):
        wt = self.squash_wt("capsule-corp-dirty")
        (wt / "n").write_text("edited")
        self.go()
        self.assertTrue(wt.exists())

    def test_squash_landed_with_an_untracked_file_is_kept(self):
        wt = self.squash_wt("capsule-corp-untracked")
        (wt / "scratch.txt").write_text("x")
        self.go()
        self.assertTrue(wt.exists())

    def test_deleted_file_that_main_also_lacks_is_removed(self):
        wt = self.squash_wt("capsule-corp-del", gone=True)
        self.go()
        self.assertFalse(wt.exists())

    def test_deleted_file_that_main_still_has_is_kept(self):
        wt = self.squash_wt("capsule-corp-delkept", gone=True)
        (self.repo / "g").write_text("g")
        self.git(self.repo, "add", "g")
        self.git(self.repo, "commit", "-q", "-m", "g back")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.go()
        self.assertTrue(wt.exists())

    def test_git_error_while_comparing_keeps_it(self):
        wt = self.squash_wt("capsule-corp-err")
        real = disk_relief.run

        def broken(cmd, *a, **k):
            if "--quiet" in cmd:
                r = real(["git", "--version"], *a, **k)
                r.returncode = 128
                return r
            return real(cmd, *a, **k)
        with mock.patch.object(disk_relief, "run", broken):
            self.go()
        self.assertTrue(wt.exists())

    def landed(self, name, files, main_files=None, detached=False, moves=()):
        """A worktree commits `files` ({path: text}); main gets the same as one squash commit,
        with `main_files` overriding, and moves on. `moves` are (old, new) renames in the worktree
        only (main gets the new path but keeps the old)."""
        wt = self.tmp / name
        args = ["--detach"] if detached else ["-b", name]
        self.git(self.repo, "worktree", "add", "-q", *args, str(wt), "main")
        for old, new in moves:
            self.git(wt, "mv", old, new)
        for side, root in ((files, wt), ({**files, **(main_files or {})}, self.repo)):
            for rel, text in side.items():
                (root / rel).write_text(text)
            if root == wt:
                self.git(wt, "add", "-A")
                self.git(wt, "commit", "-q", "-m", "work")
        for _old, new in moves:
            (self.repo / new).write_text((wt / new).read_text())
        (self.repo / "other").write_text("later work")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "squash")
        self.git(self.repo, "push", "-q", "origin", "main")
        return wt

    def test_chunk_boundaries(self):
        names = {f"c{i:03d}": "a" for i in range(401)}
        for label, bad in (("first", "c199"), ("last", "c400"), ("none", None)):
            with self.subTest(label):
                files = {f"{label}-{k}": v for k, v in names.items()}
                main = {f"{label}-{bad}": "different"} if bad else None
                wt = self.landed(f"capsule-corp-chunk-{label}", files, main)
                self.go()
                self.assertEqual(wt.exists(), bad is not None)

    def test_odd_path_names(self):
        files = {"-dash": "1", "sp ace": "2", "café-☃": "3"}
        wt = self.landed("capsule-corp-odd", files)
        self.go()
        self.assertFalse(wt.exists())
        wt = self.landed("capsule-corp-odd2", {"-dash2": "1", "sp ace2": "2"}, {"sp ace2": "x"})
        self.go()
        self.assertTrue(wt.exists())

    def test_rename_where_main_keeps_the_old_path_is_kept(self):
        wt = self.landed("capsule-corp-mv", {}, moves=[("f", "f2")])
        self.go()
        self.assertTrue(wt.exists())

    def test_detached_squash_is_kept(self):
        wt = self.landed("capsule-corp-det", {"n": "new"}, detached=True)
        self.go()
        self.assertTrue(wt.exists())

    def test_textconv_cannot_hide_a_difference(self):
        self.git(self.repo, "config", "diff.same.textconv", "echo same #")
        wt = self.landed("capsule-corp-tc", {".gitattributes": "tc* diff=same\n", "tc": "one"},
                         {"tc": "two"})
        self.go()
        self.assertTrue(wt.exists())

    def test_fetch_is_hardened(self):
        seen = []
        real = disk_relief.run

        def spy(cmd, *a, **k):
            if "fetch" in cmd:
                seen.append((cmd, k.get("env", {}), k.get("timeout")))
            return real(cmd, *a, **k)
        with mock.patch.object(disk_relief, "run", spy):
            self.go()
        cmd, env, timeout = seen[0]
        self.assertIn("--no-auto-gc", cmd)
        self.assertIn("--no-auto-maintenance", cmd)
        self.assertIn("core.sshCommand=ssh -o BatchMode=yes", cmd)
        self.assertEqual((env["GIT_TERMINAL_PROMPT"], timeout), ("0", 120))


if __name__ == "__main__":
    unittest.main()
