"""A new file on the size route is in the file set, in both briefs (inc_1517d48d)."""
import unittest

from capsule import bundles

TASK = {"id": "t_1", "title": "t", "body": "File set:\n- capsule/a.py\n"}
HEADING = "## Size route and the file set (inc_1517d48d)"


def _note(prompt):
    """The shared note's text as it sits in a prompt: from its heading to the next blank line."""
    return prompt.partition(HEADING)[2].partition("\n\n")[0] if HEADING in prompt else ""


class SizeRouteScope(unittest.TestCase):
    def test_both_briefs_put_a_size_route_new_file_in_the_file_set(self):
        impl = _note(bundles.implementer(TASK)["prompt"])
        rev = _note(bundles.reviewer(TASK, "diff")["prompt"])
        self.assertTrue(impl)
        self.assertEqual(impl, rev)
        for text in ("new file", "frozen size", "file set", "test file", "`notes`", "outside"):
            self.assertIn(text, impl)

    def test_the_note_keeps_an_existing_file_outside_the_set_as_a_hold(self):
        note = _note(bundles.implementer(TASK)["prompt"])
        self.assertIn("existing file outside the set stays a hold", note)
        self.assertIn("do not raise", note)
        self.assertIn("do not hold", note)
