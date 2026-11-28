"""The mutation gate at review judges a card against the lane's current base
(`review_scope.bases`, used by `Tick._merge_gate`): once a lane has taken main after its first
run, the review snapshot's parent is the lane's HEAD and `--base` is a synthetic merge of the
first-run commit and main, so neither a test main already holds nor an earlier card's unlanded
commit is blamed on the card (t_02bf15e2).
"""
import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import review_scope, workspace
from capsule.tick import Tick


def git(cwd, *args):
    return subprocess.run(["git", *workspace.IDENTITY, *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(cwd, name, text="x\n"):
    (Path(cwd) / name).write_text(text)
    git(cwd, "add", ".")
    git(cwd, "commit", "-qm", f"add {name}")
    return git(cwd, "rev-parse", "HEAD")


class GateBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "tests").mkdir()
        (self.origin / "tests" / "mutation_check.py").write_text("")
        commit(self.origin, "README.md")
        git(self.origin, "branch", "lane")
        self.ws = workspace.clone(str(self.origin), "lane", self.root / "work")
        self.start = workspace.head(self.ws)
        review_scope._REPORTED.clear()

    def main_adds(self, name, text="x\n"):
        git(self.origin, "checkout", "-q", "main")
        sha = commit(self.origin, name, text)
        git(self.ws, "fetch", "-q", "origin", "main")
        return sha

    def lane_merges_main(self):
        git(self.ws, "merge", "-q", "--no-ff", "--no-edit", "origin/main")

    def diff_names(self, base, parent):
        return git(self.ws, "diff", "--name-only", f"{base}..{parent}").splitlines()

    def epic_lane(self):
        """An earlier card's unlanded commit, the first run, main moves on, the lane takes it
        with a merge commit, then the card adds card.py: returns the lane HEAD."""
        commit(self.ws, "prior_card.py")
        self.start = workspace.head(self.ws)
        self.main_adds("test_main_only.py")
        self.lane_merges_main()
        return commit(self.ws, "card.py")

    def fell_back(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = review_scope.bases(self.ws, self.start)
        self.assertEqual(got, ((self.start,), self.start))
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        return err.getvalue()

    def test_an_epic_lane_range_holds_only_this_cards_files(self):
        lane_head = self.epic_lane()
        (parent, *_), base = review_scope.bases(self.ws, self.start)
        self.assertEqual(parent, lane_head)
        self.assertEqual(self.diff_names(base, parent), ["card.py"])

    def test_a_lane_that_merged_main_is_judged_from_its_head(self):
        self.main_adds("test_intent_diff_base.py")
        self.lane_merges_main()
        lane_head = commit(self.ws, "card.py")
        (parent, *_), base = review_scope.bases(self.ws, self.start)
        self.assertEqual(parent, lane_head)
        self.assertNotEqual(base, self.start)

    def test_a_test_identical_to_origin_main_is_not_in_the_range(self):
        self.main_adds("test_intent_diff_base.py")
        self.lane_merges_main()
        commit(self.ws, "card.py")
        (parent, *_), base = review_scope.bases(self.ws, self.start)
        self.assertIn("test_intent_diff_base.py", workspace.changed(self.ws, self.start))
        self.assertEqual(self.diff_names(base, parent), ["card.py"])

    def test_a_lane_that_never_merged_main_keeps_the_first_run_base(self):
        self.main_adds("later.py")
        commit(self.ws, "card.py")
        self.assertEqual(review_scope.bases(self.ws, self.start), ((self.start,), self.start))

    def test_an_unresolvable_origin_main_falls_back_and_logs_one_line(self):
        git(self.origin, "branch", "-m", "main", "trunk")
        git(self.ws, "update-ref", "-d", "refs/remotes/origin/main")
        commit(self.ws, "card.py")
        self.assertIn("fetch", self.fell_back())

    def test_the_same_fallback_is_logged_once_and_a_new_reason_logs_again(self):
        git(self.origin, "branch", "-m", "main", "trunk")
        git(self.ws, "update-ref", "-d", "refs/remotes/origin/main")
        commit(self.ws, "card.py")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            first = [review_scope.bases(self.ws, self.start) for _ in range(2)]
        self.assertEqual(first, [((self.start,), self.start)] * 2)
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        with contextlib.redirect_stderr(err), mock.patch.object(
                review_scope, "_synthetic_bases", side_effect=OSError("git is gone")):
            review_scope.bases(self.ws, self.start)
        self.assertEqual(len(err.getvalue().splitlines()), 2)

    def test_a_stale_origin_main_is_refreshed_before_the_merge_base(self):
        held = commit(self.origin, "later.py")
        self.assertNotEqual(git(self.ws, "rev-parse", "refs/remotes/origin/main"), held)
        review_scope.bases(self.ws, self.start)
        self.assertEqual(git(self.ws, "rev-parse", "refs/remotes/origin/main"), held)

    def test_a_failing_fetch_never_prints_a_credential_and_uses_the_normal_timeout(self):
        real, seen = workspace._git, []
        git(self.ws, "remote", "set-url", "origin", "https://user:SECRET@host.invalid/x")

        def failing_fetch(*args, **kw):
            if args[0] != "fetch":
                return real(*args, **kw)
            seen.append(kw.get("timeout"))
            raise RuntimeError("git fetch failed (rc=128): https://user:SECRET@host.invalid/x")
        with mock.patch.object(workspace, "_git", failing_fetch):
            out = self.fell_back()
        self.assertNotIn("SECRET", out)
        self.assertIn("fetch", out)
        self.assertEqual(seen, [None])   # `_git`'s own default (60 s), never the slow one

    def test_a_start_sha_of_none_behaves_as_before(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(workspace, "_git") as git_mock:
            got = review_scope.bases(self.ws, None)
        self.assertEqual((got, err.getvalue(), git_mock.called), (((None,), None), "", False))

    def test_a_failure_computing_the_synthetic_base_never_escapes(self):
        self.epic_lane()
        real = workspace._git
        broken = {
            "timeout": subprocess.TimeoutExpired("git merge-tree", 5),
            "no output": subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            "os error": OSError("git is gone"),
        }
        for name, fault in broken.items():
            def flaky(*args, _fault=fault, **kw):
                if "merge-tree" not in args:
                    return real(*args, **kw)
                if isinstance(_fault, BaseException):
                    raise _fault
                return _fault
            with self.subTest(name), mock.patch.object(workspace, "_git", flaky):
                self.fell_back()

    def test_a_conflicting_merge_falls_back_and_logs_one_line(self):
        commit(self.ws, "README.md", "lane\n")
        self.start = workspace.head(self.ws)
        self.main_adds("README.md", "main\n")
        subprocess.run(["git", *workspace.IDENTITY, "merge", "-q", "origin/main"], cwd=self.ws,
                       capture_output=True)
        git(self.ws, "checkout", "--ours", "README.md")
        git(self.ws, "add", "README.md")
        git(self.ws, "commit", "-qm", "resolve")
        self.assertIn("merge-tree", self.fell_back())

    def test_merge_gate_parks_a_snapshot_on_the_lane_head_against_the_base(self):
        lane_head = self.epic_lane()
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_runs (id INT, task_id TEXT, profile TEXT, metadata TEXT)")
        conn.execute("INSERT INTO task_runs VALUES (1, 't_1', 'implementer', ?)",
                     (json.dumps({"start_sha": self.start}),))
        (cmd,) = Tick._merge_gate(Tick, conn, "t_1", self.ws)
        snap = git(self.ws, "rev-parse", "refs/capsule/review")
        base = cmd.split("--base ")[1].split()[0]
        self.assertEqual(git(self.ws, "rev-parse", f"{snap}^"), lane_head)
        self.assertEqual(git(self.ws, "rev-parse", f"{snap}^2"), base)   # the fetch carries it
        self.assertEqual(self.diff_names(base, snap), ["card.py"])
        self.assertIn(f"--head {snap}", cmd)


if __name__ == "__main__":
    unittest.main()
