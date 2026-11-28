"""inc_a1947f9a: the briefs give the rule 2 and fixture-edit gate routes."""
import unittest

from capsule import bundles

TASK = {"id": "t_00000001", "title": "x", "body": "B"}


def _flat(text):
    return " ".join(text.split())


class GateRoutes(unittest.TestCase):
    def test_the_briefs_give_the_rule_2_and_fixture_edit_routes(self):
        impl = _flat(bundles.implementer(TASK)["prompt"])
        for text in ("rule 2", "leading underscore",
                     "name in a card criterion is not a reason to hold",
                     "the rename goes in `notes`",
                     "changes only its script or fixture",
                     "no assertion changes",
                     "a new test in another file fails on base",
                     "The old test stays",
                     "add the marker with the old test id",
                     "name the new test in the reason",
                     "offset the marker line in the same file",
                     "A subject changed in this range needs a test that fails on base, not a"
                     " marker."):
            self.assertIn(text, impl)
        self.assertIn("not a marker. One exception:", impl)
        korin = _flat(bundles.korin(TASK, {}, [], [])["prompt"])
        self.assertIn("A new function with fewer than two production callers takes a leading"
                      " underscore", korin)
        self.assertIn("lists each existing test that drives that behaviour, with its route", korin)


if __name__ == "__main__":
    unittest.main()
