"""Korin's recurrence test must sit in a module with room under the size cap (inc_2185fb86)."""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import bundles, incident_recurrence, incidents
from capsule.tests.test_incidents import good_doc

INC = {"id": "inc_aaaaaaaa", "kind": "crash", "root_key": "k"}
SIZE_CHECK = Path(__file__).resolve().parents[2] / "tests" / "size_check.py"


def _problems(test_id, lines=None, path="capsule/tests/test_x.py"):
    """postmortem_problems for `test_id`, on a temp repo whose main holds `path` at `lines`."""
    with tempfile.TemporaryDirectory() as root:
        run = ["git", "-C", root]
        subprocess.run(run + ["init", "-q", "-b", "main"], check=True)
        if lines is not None:
            Path(root, path).parent.mkdir(parents=True)
            Path(root, path).write_text("x = 1\n" * lines)
            subprocess.run(run + ["add", "-A"], check=True)
            who = ["-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run(run + who + ["commit", "-qm", "m"], check=True)
        with mock.patch.object(incidents.paths, "REPO", Path(root)):
            return incidents.postmortem_problems(good_doc(INC["id"], recurrence_test=test_id), INC)


class RecurrenceRoom(unittest.TestCase):
    ID = "capsule.tests.test_x.Case.test_a"

    def test_a_recurrence_test_in_a_module_at_the_size_cap_is_refused(self):
        probs = _problems(self.ID, 800)
        self.assertEqual(len(probs), 1, probs)
        for text in ("capsule/tests/test_x.py", "800", "new test module"):
            self.assertIn(text, probs[0])

    def test_a_module_of_651_lines_is_refused(self):
        self.assertEqual(len(_problems(self.ID, 651)), 1)

    def test_a_module_of_650_lines_passes(self):
        self.assertEqual(_problems(self.ID, 650), [])

    def test_a_module_absent_on_main_passes(self):
        self.assertEqual(_problems(self.ID, None), [])

    def test_a_module_over_the_cap_is_refused(self):
        probs = _problems(self.ID, 2114)
        self.assertEqual(len(probs), 1, probs)
        self.assertIn("2114", probs[0])

    def test_a_shell_id_passes(self):
        self.assertEqual(_problems("tests/test_x.sh", 900, path="tests/test_x.sh"), [])

    def test_a_malformed_id_keeps_its_existing_problem_text(self):
        probs = _problems("run the tests", 900)
        self.assertEqual(len(probs), 1, probs)
        self.assertIn("is not a dotted unittest id", probs[0])

    def test_the_file_cap_is_the_size_checks_and_the_note_has_the_sentence(self):
        spec = importlib.util.spec_from_file_location("size_check_under_test", SIZE_CHECK)
        mod = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "path", [str(SIZE_CHECK.parent), *sys.path]):
            spec.loader.exec_module(mod)
        self.assertEqual(incident_recurrence.FILE_CAP, mod.FILE_LIMIT)
        self.assertIn("Name a new test module when the target module has more than 650 lines.",
                      bundles.KORIN_NOTE)
