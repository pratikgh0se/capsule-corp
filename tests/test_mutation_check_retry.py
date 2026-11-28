"""Zenkai Z1/Z2 for tests/mutation_check.py gate (b) attempt 2 (code and intent examples in one
range): fixture repos built fresh in tempfile; helpers come from test_mutation_check."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc  # noqa: E402
from test_mutation_check import (  # noqa: E402
    DISCRIMINATING_TEST, PLACEHOLDER_TEST, PROD_BUGGY, PROD_FIXED,
    commit_all, init_repo, write_empty_baseline, write_intent_fixture, write_test)

PASS_TEST = (
    "import unittest\n\n\nclass T(unittest.TestCase):\n"
    "    def test_x(self):\n        pass\n"
)
GOOD = "FAIL (good) [revert: production only; contracts data kept at HEAD]"

LOOP_TEST = (
    "import unittest\n"
    "from production import add\n"
    "from intent import ids\n\n\n"
    "class Loop(unittest.TestCase):\n"
    "    def test_every_example_matches_its_oracle(self):\n"
    "        for i in ids('guide'):\n"
    "            if i == 'new-example':\n"
    "                self.assertEqual(add(2, 3), 5, i)\n"
)
VACUOUS_LOOP_TEST = LOOP_TEST.replace(
    "self.assertEqual(add(2, 3), 5, i)", "self.assertTrue(callable(add), i)")


def make_code_and_example_range(root, test_src):
    """Base: buggy code, one example. Head: the fix, a new example and a loop test over examples."""
    init_repo(root)
    write_empty_baseline(root)
    write_intent_fixture(root, ["{\"id\": \"old-example\"}"])
    (root / "production.py").write_text(PROD_BUGGY)
    write_test(root, PASS_TEST)
    base = commit_all(root, "base")
    write_intent_fixture(root, ["{\"id\": \"old-example\"}", "{\"id\": \"new-example\"}"])
    (root / "production.py").write_text(PROD_FIXED)
    write_test(root, test_src)
    return base, commit_all(root, "fix plus new example plus loop test")


class CodeAndExampleInOneRange(unittest.TestCase):
    """The intent examples a test executes are an oracle, not production: when the combined revert
    reads "did not fail" and the range has both code and data, retry with only the code reverted."""

    def test_loop_test_vacuous_under_combined_revert_is_good_under_prod_only_revert(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = make_code_and_example_range(root, LOOP_TEST)
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertEqual(rows[0][2], GOOD)

    def test_a_test_that_passes_both_ways_is_still_bad(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = make_code_and_example_range(root, VACUOUS_LOOP_TEST)
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            self.assertEqual(rows[0][2], "did not fail (BAD)")

    def test_a_code_only_range_is_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertTrue(ok, rows)
            self.assertEqual(rows[0][2], "FAIL (good)")


class AgentMarkersDropped(unittest.TestCase):
    """Every subprocess the gate spawns runs without the agent-session markers `capsule human`
    refuses on, so a test passes or fails the same whoever runs the gate."""

    def test_sh_hands_its_child_no_agent_markers_and_keeps_the_rest(self):
        probe = ("import os; print(os.environ.get('CLAUDECODE'), os.environ.get('AI_AGENT'),"
                 " os.environ.get('KEEP_ME'))")
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "AI_AGENT": "x", "KEEP_ME": "y"}):
            rc, out = mc.sh([sys.executable, "-c", probe])
        self.assertEqual((rc, out.strip()), (0, "None None y"))


HELPER_SRC = "def noop():\n    pass\n"
KEEP_TEST = (
    "import unittest\nfrom production import add\nimport helper\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n        helper.noop()\n        self.assertEqual(add(2, 3), 5)\n"
)
SHELL_TEST = (
    "import subprocess\nimport unittest\nfrom pathlib import Path\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_prefix(self):\n"
    "        root = Path(__file__).resolve().parents[1]\n"
    "        out = subprocess.run(['sh', str(root / 'bin' / 'lib_prefix.sh')],\n"
    "                             capture_output=True, text=True).stdout.strip()\n"
    "        self.assertEqual(out, 'new')\n"
)


class B100RevertFalseNegatives(unittest.TestCase):
    def test_new_helper_module_is_kept_while_other_product_files_are_reverted(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_empty_baseline(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PASS_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            (root / "helper.py").write_text(HELPER_SRC)
            write_test(root, KEEP_TEST)
            head = commit_all(root, "fix plus new helper plus test")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertTrue(rows[0][2].startswith("FAIL (good)"), rows)

    def test_changed_shell_product_file_is_reverted_to_base(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_empty_baseline(root)
            (root / "bin").mkdir()
            (root / "bin" / "lib_prefix.sh").write_text("echo old\n")
            write_test(root, PASS_TEST)
            base = commit_all(root, "base")
            (root / "bin" / "lib_prefix.sh").write_text("echo new\n")
            write_test(root, SHELL_TEST)
            head = commit_all(root, "change shell lib plus test")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertTrue(rows[0][2].startswith("FAIL (good)"), rows)

    def test_new_helper_does_not_turn_a_pinned_nothing_test_good(self):
        """A range adds `greet()` and a helper; the test touches the helper and only checks that
        `production.greet` exists. Reverting the pure addition makes it fail on a missing name,
        which is not evidence the test pins anything: BAD."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_empty_baseline(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PASS_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_BUGGY + "\n\ndef greet():\n    return 'hi'\n")
            (root / "helper.py").write_text(HELPER_SRC)
            write_test(root, (
                "import unittest\nimport helper\nimport production\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_greet_exists(self):\n"
                "        helper.noop()\n"
                "        self.assertIsNotNone(production.greet)\n"))
            head = commit_all(root, "add greet plus helper plus weak test")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            self.assertTrue(rows[0][2].startswith("did not fail (BAD)"), rows)

    def test_extensionless_bin_wrapper_and_its_maintenance_lib_are_both_reverted(self):
        """Both change; reverting only the lib would leave a head wrapper calling a head-only
        function, a false FAIL. Both at base the test passes: BAD."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_empty_baseline(root)
            (root / "bin").mkdir()
            (root / "maintenance").mkdir()
            (root / "maintenance" / "lib_wrap.sh").write_text("say_old() { echo old; }\n")
            (root / "bin" / "wrap-it").write_text(
                ". \"$(dirname \"$0\")/../maintenance/lib_wrap.sh\"\nsay_old\n")
            write_test(root, PASS_TEST)
            base = commit_all(root, "base")
            (root / "maintenance" / "lib_wrap.sh").write_text("say_new() { echo new; }\n")
            (root / "bin" / "wrap-it").write_text(
                ". \"$(dirname \"$0\")/../maintenance/lib_wrap.sh\"\nsay_new\n")
            write_test(root, (
                "import subprocess\nimport unittest\nfrom pathlib import Path\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_wrap_it_runs(self):  # bin/wrap-it sources maintenance/lib_wrap.sh\n"
                "        root = Path(__file__).resolve().parents[1]\n"
                "        rc = subprocess.run(['sh', str(root / 'bin' / 'wrap-it')],\n"
                "                            capture_output=True).returncode\n"
                "        self.assertEqual(rc, 0)\n"))
            head = commit_all(root, "change wrapper and lib plus test")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            self.assertTrue(rows[0][2].startswith("did not fail (BAD)"), rows)

    def test_unrelated_new_module_is_not_stubbed_in_place_of_the_real_subject(self):
        """The test needs a function-less new helper; the real subject is a changed module with a
        pure addition. An unrelated new module with a function must not make the stub step run
        at head (where the subject is unreverted) and read BAD."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_empty_baseline(root)
            (root / "production.py").write_text("def brief():\n    return 'base'\n")
            write_test(root, PASS_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(
                "def flag():\n    return 'WARN'\n\n\ndef brief():\n    return flag() + ' head'\n")
            (root / "pkg_tests_helper.py").write_text("VALUE = 1\n")
            (root / "unrelated.py").write_text(HELPER_SRC)
            write_test(root, (
                "import unittest\nimport pkg_tests_helper\nimport production\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_brief(self):\n"
                "        production.flag()\n"
                "        self.assertEqual(production.brief(), 'WARN head')\n"))
            head = commit_all(root, "pure addition plus helper plus unrelated module plus test")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertFalse(rows[0][2].startswith("did not fail (BAD)"), rows)

    def test_a_test_naming_a_longer_shell_file_does_not_reach_the_changed_one(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_empty_baseline(root)
            (root / "bin").mkdir()
            (root / "bin" / "lib_prefix.sh").write_text("echo old\n")
            write_test(root, PASS_TEST)
            base = commit_all(root, "base")
            (root / "bin" / "lib_prefix.sh").write_text("echo new\n")
            write_test(root, SHELL_TEST.replace("lib_prefix.sh", "mylib_prefix.sh"))
            head = commit_all(root, "change shell lib plus test of another file")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertEqual(rows[0][2], "n/a (no production change)", rows)


if __name__ == "__main__":
    unittest.main()
