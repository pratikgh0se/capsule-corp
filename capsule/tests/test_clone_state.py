"""inc_1abc7a24: the implementer brief names the uncommitted paths the clone carries."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import clone_state, worker, workspace
from capsule.tests import tmpclean

TASK = {"id": "t_0000aaaa", "title": "t", "body": "do it"}


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(cwd),
                   check=True, capture_output=True, text=True)


def porcelain(ws):
    return workspace._git("status", "--porcelain", ws=ws).stdout


class CloneStateBrief(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, base)
        origin = base / "origin"
        (origin / "docs").mkdir(parents=True)
        git(origin, "init", "-q", "-b", "main")
        for name in ("docs/ZENKAI.md", "test_a.py", "old.txt"):
            (origin / name).write_text("x\n")
        git(origin, "add", ".")
        git(origin, "commit", "-qm", "init")
        self.ws = workspace.clone(str(origin), "main", base / "root")

    def brief(self):
        return worker._bundle(None, "implementer", TASK, {"resumed_from": None}, {},
                              workspace_dir=str(self.ws))["prompt"]

    def test_a_rerun_brief_names_the_uncommitted_paths_the_clone_carries(self):
        (self.ws / "docs" / "ZENKAI.md").write_text("stale\n")
        git(self.ws, "add", "docs/ZENKAI.md")
        (self.ws / "test_a.py").write_text("work\n")
        before = porcelain(self.ws)
        prompt = self.brief()
        self.assertEqual(porcelain(self.ws), before)
        section = prompt.split("## Clone state at dispatch")[1].split("\n\n")[0]
        self.assertIn("predate", section)
        lines = {ln.split(" ")[1]: ln for ln in section.splitlines() if ln.startswith("- ")}
        self.assertIn("do not edit it and do not hold the card for it", lines["docs/ZENKAI.md"])
        self.assertIn("the next verdict restores it", lines["docs/ZENKAI.md"])
        self.assertIn("earlier work on this card", lines["test_a.py"])
        self.assertIn("keep it unless the card or review says otherwise", lines["test_a.py"])

    def test_a_long_list_is_capped_with_a_count_of_the_rest(self):
        for i in range(25):
            (self.ws / f"f{i:02}.txt").write_text("w\n")
        section = self.brief().split("## Clone state at dispatch")[1].split("\n\n")[0]
        self.assertEqual(len([ln for ln in section.splitlines() if ln.startswith("- ")]), 20)
        self.assertIn("... and 5 more", section)

    def test_a_clean_clone_adds_no_section(self):
        self.assertNotIn("## Clone state at dispatch", self.brief())

    def test_a_missing_workspace_adds_no_section(self):
        prompt = worker._bundle(None, "implementer", TASK, {"resumed_from": None}, {})["prompt"]
        self.assertNotIn("## Clone state at dispatch", prompt)

    def test_a_renamed_path_and_a_non_ascii_path_keep_their_real_names(self):
        git(self.ws, "mv", "old.txt", "new name.txt")
        (self.ws / "café.txt").write_text("y\n")
        found = {c["path"]: c["state"] for c in clone_state.carried(self.ws)}
        self.assertEqual(found, {"new name.txt": "staged", "café.txt": "untracked"})

    def test_a_non_git_workspace_gives_unknown_and_no_exception(self):
        plain = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, plain)
        self.assertIsNone(clone_state.carried(plain))
        prompt = worker._bundle(None, "implementer", TASK, {"resumed_from": None}, {},
                                workspace_dir=str(plain))["prompt"]
        self.assertNotIn("## Clone state at dispatch", prompt)

    def test_a_git_failure_gives_unknown_and_no_exception(self):
        for exc in (RuntimeError("boom"), OSError("gone"),
                    subprocess.TimeoutExpired("git", 1)):
            with mock.patch.object(workspace, "_git", side_effect=exc):
                self.assertIsNone(clone_state.carried(self.ws))


if __name__ == "__main__":
    unittest.main()
