"""capsule/lander_select.py: the lander runs selected tests, or the board's own commands (D273).
Zenkai Z1: contracts/intent/lander_select.jsonl is replayed in `Intent` below."""
import json
import subprocess
import tempfile
import unittest
from unittest import mock
from collections import namedtuple
from pathlib import Path

from capsule import lander_select, shards

Work = namedtuple("Work", "path")
CMDS = ["python3 -m unittest discover -s capsule/tests -t .", "bash tests/run_shell_suites.sh",
        "echo lint"]
CFG = {"contract_commands": CMDS}
ON = {"select_tests": True}


class Rewrite(unittest.TestCase):
    def test_discovery_and_shell_runner_are_narrowed(self):
        got = lander_select._rewrite(CMDS, ["capsule.tests.test_a", "maintenance.tests.test_b",
                                           "tests/test_c.sh"])
        self.assertEqual(got, ["python3 -m unittest capsule.tests.test_a",
                               "bash tests/run_shell_suites.sh --only 'tests/test_c.sh'",
                               "echo lint"])

    def test_nothing_selected_keeps_only_other_commands(self):
        self.assertEqual(lander_select._rewrite(CMDS, []), ["echo lint"])


class KnownDirsOnly(unittest.TestCase):
    def test_a_discovery_of_a_directory_the_selector_does_not_know_is_kept_whole(self):
        cmds = ["python3 -m unittest discover -s tests/ui -t .", CMDS[0]]
        self.assertEqual(lander_select._rewrite(cmds, ["capsule.tests.test_a"]),
                         ["python3 -m unittest discover -s tests/ui -t .",
                          "python3 -m unittest capsule.tests.test_a"])


class Parts(unittest.TestCase):
    def repo(self, change):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)

        def git(*a):
            return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c",
                                   "user.email=t@t.invalid", *a], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        (d / "capsule/tests").mkdir(parents=True)
        (d / "capsule/bar.py").write_text("Y = 1\n")
        (d / "capsule/paths.py").write_text("P = 1\n")
        (d / "capsule/tests/test_bar.py").write_text("import unittest\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD")
        (d / change).parent.mkdir(parents=True, exist_ok=True)
        (d / change).write_text("changed = 1\n")
        git("add", "-A")
        git("commit", "-qm", "change")
        return Work(d), base, git("rev-parse", "HEAD")

    def test_a_small_diff_runs_only_its_tests(self):
        work, base, head = self.repo("capsule/bar.py")
        got = lander_select.parts(CFG, ON, work, base, head)
        self.assertEqual(got[0], "python3 -m unittest capsule.tests.test_bar")

    def test_unmappable_diff_runs_the_boards_commands(self):
        work, base, head = self.repo("capsule/paths.py")
        self.assertIsNone(lander_select.parts(CFG, ON, work, base, head))

    def test_selection_is_off_unless_the_board_turns_it_on(self):
        work, base, head = self.repo("capsule/bar.py")
        self.assertIsNone(lander_select.parts(CFG, {}, work, base, head))

    def test_a_sql_change_runs_the_boards_commands(self):
        work, base, head = self.repo("capsule/schema.sql")
        self.assertIsNone(lander_select.parts(CFG, ON, work, base, head))

    def test_a_docs_only_change_runs_no_discovery(self):
        work, base, head = self.repo("docs/notes.md")
        got = lander_select.parts(CFG, ON, work, base, head)
        self.assertEqual(got, ["echo lint"])

    def test_intent_rows(self):
        path = Path(__file__).resolve().parents[2] / "contracts" / "intent" / "lander_select.jsonl"
        for line in path.read_text().splitlines():
            row = json.loads(line)
            doc = row["given"]["document"]
            work, base, head = self.repo(doc["change"])
            got = lander_select.parts(CFG, {"select_tests": doc["select_tests"]}, work, base, head)
            kind = "full" if got is None else "picked"
            self.assertEqual(kind, row["expect"]["kind"], row["id"])

    def test_a_selector_timeout_or_crash_runs_the_boards_commands(self):
        work, base, head = self.repo("capsule/bar.py")
        for error in (subprocess.TimeoutExpired("select", 1), OSError("no python")):
            with mock.patch.object(subprocess, "run", side_effect=error):
                self.assertIsNone(lander_select.parts(CFG, ON, work, base, head))

    def test_flag_off_runs_the_boards_commands(self):
        work, base, head = self.repo("capsule/bar.py")
        self.assertIsNone(lander_select.parts(CFG, {"select_tests": False}, work, base, head))

    def test_board_shards_survive_a_full_fallback(self):
        work, base, head = self.repo("capsule/paths.py")
        cfg = {**CFG, "contract_shards": {CMDS[0]: 2}}
        got = lander_select.parts(cfg, ON, work, base, head)
        self.assertEqual(len(got), 4)
        self.assertEqual(got, shards.expand(cfg))


if __name__ == "__main__":
    unittest.main()
