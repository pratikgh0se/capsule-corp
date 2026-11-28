"""maintenance/public_export.py (P10a), DEST safety: history must be export-only, only tracked files
are deleted, refusals leave DEST as it was. Fixture repos only."""
import hashlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import public_export  # noqa: E402
from public_export_fixture import Fixture, git  # noqa: E402


class Refusals(Fixture):
    def test_unknown_sha(self):
        rc, _, err = self.export("deadbeef" * 5)
        self.assertEqual(rc, 2)
        self.assertIn("not a commit", err)

    def test_dest_inside_private_repo(self):
        rc, _, err = self.export(self.sha1, dest=self.repo / "out")
        self.assertEqual(rc, 2)
        self.assertIn("overlaps", err)

    def test_dest_is_the_private_repo(self):
        rc, _, _ = self.export(self.sha1, dest=self.repo)
        self.assertEqual(rc, 2)

    def test_dest_nonempty_and_not_a_checkout(self):
        self.dest.mkdir()
        (self.dest / "keep.txt").write_text("mine")
        rc, _, err = self.export(self.sha1)
        self.assertEqual(rc, 2)
        self.assertIn("not the root of a git checkout", err)
        self.assertTrue((self.dest / "keep.txt").exists())

    def test_dest_with_uncommitted_changes(self):
        self.export(self.sha1)
        (self.dest / "scratch.txt").write_text("wip")
        rc, _, err = self.export(self.sha1)
        self.assertEqual(rc, 2)
        self.assertIn("uncommitted", err)
        self.assertTrue((self.dest / "scratch.txt").exists())

    def test_subdirectory_of_a_checkout_is_refused(self):
        self.export(self.sha1)
        rc, _, _ = self.export(self.sha1, dest=self.dest / "capsule")
        self.assertEqual(rc, 2)

    def test_repo_without_allowlist(self):
        git(self.repo, "rm", "-q", "release/public-paths.txt")
        sha = self.commit("no allowlist")
        rc, _, err = self.export(sha)
        self.assertEqual(rc, 2)
        self.assertIn("cannot build the public tree", err)


class DestHistory(Fixture):
    """H1: DEST history must be export-only."""

    def test_clone_of_private_repo_is_refused(self):
        git(self.root, "clone", "-q", str(self.repo), str(self.dest))
        rc, _, err = self.export(self.sha1)
        self.assertEqual(rc, 2)
        self.assertIn("exists in the private repo", err)
        self.assertEqual(git(self.dest, "rev-list", "--count", "HEAD"), "1")

    def test_sibling_worktree_of_private_repo_is_refused(self):
        git(self.repo, "worktree", "add", "-q", "-b", "other", str(self.dest))
        rc, _, err = self.export(self.sha1)
        self.assertEqual(rc, 2)
        self.assertIn("shares the private repo's git directory", err)

    def test_foreign_commit_in_dest_is_refused(self):
        self.dest.mkdir()
        git(self.dest, "init", "-q", "-b", "main")
        git(self.dest, "config", "user.email", "me@private.example")
        git(self.dest, "config", "user.name", "Me")
        (self.dest / "x.txt").write_text("x")
        git(self.dest, "add", "-A")
        git(self.dest, "commit", "-q", "-m", "mine")
        rc, _, err = self.export(self.sha1)
        self.assertEqual(rc, 2)
        self.assertIn("not made by the export identity", err)
        self.assertEqual(self.names(), ["x.txt"])

    def test_a_git_failure_reading_dest_history_is_a_refusal(self):
        self.export(self.sha1)
        real = public_export._git
        for failing in ("rev-list", "log"):
            def flaky(cwd, *args, **kw):
                if failing in args and "--all" in args:
                    raise subprocess.CalledProcessError(128, "git", stderr=b"boom")
                return real(cwd, *args, **kw)
            self.put("capsule/a.py", f"x = '{failing}'")
            sha = self.commit(failing)
            with self.subTest(failing=failing), mock.patch.object(public_export, "_git", flaky):
                rc, _, err = self.export(sha)
            self.assertEqual(rc, 2)
            self.assertIn("cannot read DEST history", err)
            self.assertNotIn(failing, (self.dest / "capsule/a.py").read_text())
        self.assertEqual(git(self.dest, "rev-list", "--count", "HEAD"), "1")

    def test_new_and_export_made_dest_are_fine(self):
        self.assertEqual(self.export(self.sha1)[0], 0)
        self.put("capsule/a.py", "x = 2")
        self.assertEqual(self.export(self.commit("two"))[0], 0)


class DestSafety(Fixture):
    """H3, L3, L4: only tracked files are ever deleted; nothing else in DEST is touched."""

    def setUp(self):
        super().setUp()
        self.put("release/public-paths.txt", "capsule/\nREADME.md\n.gitignore\n")
        self.put(".gitignore", ".venv/\nnested/\n")
        self.sha1 = self.commit("with gitignore")
        self.export(self.sha1)

    def test_ignored_files_and_nested_repo_survive_a_delisting_export(self):
        (self.dest / ".venv").mkdir()
        (self.dest / ".venv" / "precious").write_text("keep")
        (self.dest / "nested").mkdir()
        git(self.dest / "nested", "init", "-q")
        (self.dest / "nested" / "work.txt").write_text("keep")
        (self.dest / "capsule" / "kept.log").write_text("ignored? no: untracked")
        self.assertEqual(git(self.dest, "status", "--porcelain").count("kept.log"), 1)
        (self.dest / "capsule" / "kept.log").unlink()
        (self.dest / "capsule" / "x.pyc").write_text("keep")
        self.put(".gitignore", ".venv/\nnested/\n*.pyc\n")
        self.put("release/public-paths.txt", "README.md\n.gitignore\n")  # capsule/ de-listed
        sha2 = self.commit("delist capsule")
        # x.pyc is untracked and not ignored in DEST until .gitignore syncs: refuse, keep everything
        self.assertEqual(self.export(sha2)[0], 2)
        (self.dest / "capsule" / "x.pyc").unlink()
        (self.dest / "capsule" / "ignored.txt").write_text("keep")
        git(self.dest, "config", "core.excludesFile", str(self.root / "excl"))
        (self.root / "excl").write_text("ignored.txt\n")
        self.assertEqual(self.export(sha2)[0], 0)
        self.assertFalse((self.dest / "capsule" / "a.py").exists())
        self.assertEqual((self.dest / ".venv" / "precious").read_text(), "keep")
        self.assertEqual((self.dest / "nested" / "work.txt").read_text(), "keep")
        self.assertTrue((self.dest / "nested" / ".git").is_dir())
        self.assertEqual((self.dest / "capsule" / "ignored.txt").read_text(), "keep")  # dir kept

    def test_untracked_file_with_status_hidden_is_refused_and_kept(self):
        git(self.dest, "config", "status.showUntrackedFiles", "no")
        (self.dest / "notes.txt").write_text("mine")
        rc, _, err = self.export(self.sha1)
        self.assertEqual(rc, 2)
        self.assertIn("uncommitted", err)
        self.assertEqual((self.dest / "notes.txt").read_text(), "mine")

    def test_add_over_an_ignored_file_is_refused(self):
        (self.dest / ".venv").mkdir()
        self.put(".venv/new.txt", "x")  # private repo file, not allowlisted
        self.put("release/public-paths.txt", "capsule/\nREADME.md\n.gitignore\n.venv/\n")
        sha2 = self.commit("venv public")
        (self.dest / ".venv" / "new.txt").write_text("precious")
        rc, _, err = self.export(sha2)
        self.assertEqual(rc, 2)
        self.assertIn("untracked or ignored", err)
        self.assertEqual((self.dest / ".venv" / "new.txt").read_text(), "precious")

    def test_ignored_by_dest_gitignore_new_file_is_still_committed(self):
        self.put("capsule/x.pyc", "x")
        self.put(".gitignore", ".venv/\nnested/\n*.pyc\n")
        sha2 = self.commit("pyc")
        self.assertEqual(self.export(sha2)[0], 0)
        self.assertIn("capsule/x.pyc", git(self.dest, "ls-files"))

    def test_dest_is_a_file_exits_2(self):
        f = self.root / "afile"
        f.write_text("x")
        rc, _, err = self.export(self.sha1, dest=f)
        self.assertEqual(rc, 2)
        self.assertIn("not a directory", err)
        self.assertEqual(f.read_text(), "x")

    def test_status_takes_no_optional_locks(self):
        calls = []
        real = subprocess.run

        def spy(cmd, *a, **k):
            calls.append(cmd)
            return real(cmd, *a, **k)
        with mock.patch.object(public_export.subprocess, "run", spy):
            self.export(self.sha1)
        status = [c for c in calls if "status" in c]
        self.assertTrue(status)
        self.assertTrue(all("--no-optional-locks" in c for c in status))


def line(name, text):
    return f"{hashlib.sha256((text + chr(10)).encode()).hexdigest()}  {name}\n"


class SumsStayHonest(Fixture):
    def test_lines_for_unshipped_files_are_dropped(self):
        self.put("release/public-paths.txt", "contracts/\n!contracts/intent/held.jsonl\n")
        self.put("contracts/intent/kept.jsonl", "row")
        self.put("contracts/intent/held.jsonl", "held row")
        sums = line("kept.jsonl", "row") + line("held.jsonl", "held row")
        (self.repo / "contracts/intent/SHA256SUMS").write_text(sums)
        rc, _, err = self.export(self.commit("sums"))
        self.assertEqual(rc, 0, err)
        out = (self.dest / "contracts/intent/SHA256SUMS").read_text()
        self.assertEqual(out, line("kept.jsonl", "row"))
        self.assertNotIn("held", out)
        for entry in out.splitlines():
            digest, name = entry.split()
            data = (self.dest / "contracts/intent" / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)

    def test_a_name_with_a_space_is_kept_when_shipped_and_dropped_when_not(self):
        """The name is everything after the digest, as capsule/preflight.py reads it."""
        self.put("release/public-paths.txt", "contracts/\n!contracts/intent/held row.jsonl\n")
        self.put("contracts/intent/kept row.jsonl", "row")
        self.put("contracts/intent/held row.jsonl", "held row")
        sums = line("kept row.jsonl", "row") + line("held row.jsonl", "held row")
        (self.repo / "contracts/intent/SHA256SUMS").write_text(sums)
        rc, _, err = self.export(self.commit("sums with spaces"))
        self.assertEqual(rc, 0, err)
        out = (self.dest / "contracts/intent/SHA256SUMS").read_text()
        self.assertEqual(out, line("kept row.jsonl", "row"))

