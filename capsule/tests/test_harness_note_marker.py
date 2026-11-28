"""inc_204f2e86: the implementer prompt names the targets-predate-range marker route."""
import re
import unittest

from capsule import bundles, lander

TRIGGER = "did not fail (BAD)"
SHAPE_RE = re.compile(r"`(# mutation-check: targets-predate-range [^`]*)`")
LIMIT = "recurrence test must still fail on base"


class MarkerRoute(unittest.TestCase):
    def test_the_implementer_prompt_gives_the_marker_trigger_and_shape(self):
        prompt = bundles.implementer({"id": "t_00000001", "title": "x", "body": "B"})["prompt"]
        self.assertIn(TRIGGER, prompt)
        self.assertIn(":: Class.test_name", prompt)
        self.assertIn(LIMIT, prompt)
        self.assertIn("not a marker", prompt)
        found = SHAPE_RE.search(bundles.HARNESS_NOTE)
        self.assertIsNotNone(found, "HARNESS_NOTE has no backticked marker shape")
        line = "+" + found.group(1).replace("<sha>", "0123abc")
        self.assertRegex(line, lander.MARKER)
        self.assertIn("file:line", found.group(1))


if __name__ == "__main__":
    unittest.main()
