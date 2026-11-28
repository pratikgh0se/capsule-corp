"""inc_bc9cf908: the implementer brief routes in-scope work and met criteria out of left_undone."""
import unittest

from capsule import bundles

TASK = {"id": "t_00000001", "title": "x", "body": "B"}
LINES = (
    "A test or code change that the card requires and your file set allows is work to do, not a"
    " question: write it before you report, and put a line in `left_undone` only when you name"
    " what stops you.",
    "A card criterion already met on your base goes in `notes` with its commit sha, never in"
    " `left_undone`.",
    "A criterion you met by a method other than the one the card names goes in `notes` with the"
    " method and the test id, never in `left_undone`: the reviewer judges it.",
)
HOLD_LINE = "`left_undone` holds only work the card requires and you did not do."


def _flat(text):
    return " ".join(text.split())


class HoldRuleMethod(unittest.TestCase):
    def test_the_implementer_brief_routes_a_criterion_met_by_another_method_to_notes(self):
        note = _flat(bundles.HARNESS_NOTE)
        for line in LINES:
            self.assertIn(line, note)

    def test_the_implementer_brief_keeps_the_left_undone_sentence(self):
        self.assertIn(HOLD_LINE, _flat(bundles.implementer(TASK)["prompt"]))


if __name__ == "__main__":
    unittest.main()
