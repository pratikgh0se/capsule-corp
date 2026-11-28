"""inc_5233fc75: the briefs and the runbook state rule 2 for a one-caller def in another file."""
import sys
import unittest
from pathlib import Path

from capsule.tests.public_tree import private_only
from capsule import bundles

ROOT = Path(__file__).resolve().parents[2]

HARNESS_LINES = (
    "Rule 2 reads the caller's file: a new def with one caller in another file is a hard"
    " finding, with or without a leading underscore.",
    "Route: put that def in the caller's file with a leading underscore and offset its lines"
    " there; a file at the 800-line cap with no baseline key gets no exemption.",
    "Code in a new file still needs two production callers, or one caller in a frozen file.",
)
KORIN_LINE = ("A fix card never orders a new def with one production caller in another file;"
              " place the def in the caller's file, or name two production callers.")
RUNBOOK_LINE = ("a private def with one caller in its own file reports as info; a caller in"
                " another file is hard")


def _flat(text):
    return " ".join(text.split())


class CrossFileRoute(unittest.TestCase):
    @private_only
    def test_the_briefs_name_the_route_for_a_one_caller_def_in_another_file(self):
        sys.path.insert(0, str(ROOT / "tests"))
        self.addCleanup(sys.path.remove, str(ROOT / "tests"))
        from test_simplicity_check import EMPTY, run_files
        harness = _flat(bundles.HARNESS_NOTE)
        for line in HARNESS_LINES:
            self.assertIn(line, harness)
        self.assertIn(KORIN_LINE, _flat(bundles.KORIN_NOTE))
        self.assertIn(RUNBOOK_LINE, _flat((ROOT / "docs" / "RUNBOOK.md").read_text()))
        ok, report = run_files(EMPTY, {"m.py": "def _once():\n    return 1\n",
                                       "a.py": "from m import _once\n_once()\n"})
        self.assertFalse(ok, report)
        self.assertIn("[ast, hard]", report)
        same = "def _helper():\n    return 1\n\n\ndef api():\n    return _helper()\n"
        ok, report = run_files(EMPTY, {"m.py": same,
                                       "a.py": "from m import api\napi()\napi()\n"})
        self.assertTrue(ok, report)
        self.assertIn("rule 2: _helper", report)
        self.assertIn("[ast, info]", report)


if __name__ == "__main__":
    unittest.main()
