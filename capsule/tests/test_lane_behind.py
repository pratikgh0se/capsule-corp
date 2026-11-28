"""Card t_0bea545f (inc_f6e24ab9): the implementer brief names the commits on main that a held
epic lane lacks. Temp git repos only; the clone is never changed."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import worker, workspace
from capsule.tests import tmpclean

BRANCH = "epic/my-app"
TASK = {"id": "t_0bea545f", "title": "t", "body": "do the thing"}
RUN = {"resumed_from": None}


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(cwd),
                          check=True, capture_output=True, text=True).stdout


class LaneBehind(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.origin = self.dir / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "README.md").write_text("main\n")
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-qm", "init")
        git(self.origin, "branch", BRANCH)
        self.ws = workspace.clone(str(self.origin), BRANCH, self.dir / "root")

    def main_commit(self, path="capsule/tests/no_digest.py", msg="add the digest stub"):
        (self.origin / path).parent.mkdir(parents=True, exist_ok=True)
        (self.origin / path).write_text("stub\n")
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-qm", msg)
        return git(self.origin, "rev-parse", "main").strip()

    def prompt(self):
        bundle = worker._bundle(None, "implementer", TASK, RUN, {}, workspace_dir=str(self.ws))
        return bundle["prompt"]

    def test_a_held_lane_behind_main_names_the_missing_commits_in_the_brief(self):
        sha = self.main_commit()
        prompt = self.prompt()
        self.assertIn("## Lane behind main", prompt)
        self.assertIn(sha[:12], prompt)
        self.assertIn("capsule/tests/no_digest.py", prompt)
        self.assertIn("add the digest stub", prompt)
        self.assertIn("host_only", prompt)

    def test_a_lane_behind_main_with_uncommitted_work_gets_both_sections(self):
        self.main_commit()
        (self.ws / "earlier.txt").write_text("work\n")
        prompt = self.prompt()
        self.assertIn("## Lane behind main", prompt)
        self.assertIn("## Clone state at dispatch", prompt)
        self.assertIn("- earlier.txt (untracked)", prompt)

    def test_a_clone_that_contains_main_gets_no_section(self):
        self.assertNotIn("## Lane behind main", self.prompt())

    def test_behind_main_leaves_head_and_the_work_tree_unchanged(self):
        self.main_commit()
        head = workspace.head(self.ws)
        (self.ws / "wip.txt").write_text("card work\n")
        status = workspace._git("status", "--porcelain", ws=self.ws).stdout
        found = worker._behind_main(self.ws)
        self.assertEqual(found["total"], 1)
        self.assertEqual(workspace.head(self.ws), head)
        self.assertEqual(workspace._git("status", "--porcelain", ws=self.ws).stdout, status)

    def test_a_git_failure_gives_the_unknown_line_and_no_exception(self):
        boom = RuntimeError("git fetch failed: boom")
        with mock.patch.object(workspace, "_git", side_effect=boom):
            self.assertIsNone(worker._behind_main(self.ws))
            prompt = self.prompt()
        self.assertIn("## Lane behind main", prompt)
        self.assertIn("unknown", prompt)

    def test_an_oserror_gives_the_unknown_line(self):
        with mock.patch.object(workspace, "_git", side_effect=FileNotFoundError("git")):
            self.assertIsNone(worker._behind_main(self.ws))

    def test_merge_commits_take_no_slot(self):
        git(self.origin, "checkout", "-q", "-b", "side")
        (self.origin / "side.txt").write_text("s\n")
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-qm", "side work")
        git(self.origin, "checkout", "-q", "main")
        self.main_commit(path="m.txt", msg="main work")
        git(self.origin, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.assertEqual(worker._behind_main(self.ws)["total"], 2)

    def test_a_remote_without_main_gives_an_empty_list(self):
        git(self.origin, "branch", "-m", "main", "trunk")
        self.assertEqual(worker._behind_main(self.ws), [])

    def test_more_than_twenty_commits_list_twenty_and_state_the_total(self):
        for i in range(22):
            self.main_commit(path=f"f{i}.txt", msg=f"main commit {i}")
        prompt = self.prompt()
        self.assertIn("lacks 22 commit(s)", prompt)
        self.assertEqual(prompt.count("main commit "), 20)


if __name__ == "__main__":
    unittest.main()
