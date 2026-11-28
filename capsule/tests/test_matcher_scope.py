"""inc_03ac215a: a card that asks for a phrase matcher names a closed list of word forms, and the
reviewer judges the matcher on those forms only; Korin's fix card carries the same finish line."""
import unittest

from capsule import bundles

TASK = {"id": "t", "title": "t", "body": ""}
SCOPE_RULES = ("the forms the card names are the acceptance list",
               "Fail a named form that the matcher misses", "Put an unnamed form in `notes`",
               "`safety: true` or class `security` still fails")
FIX_CARD_RULE = 'names a closed list of literal lines, never "at least" or "such as"'


class MatcherScope(unittest.TestCase):
    def test_the_reviewer_and_korin_briefs_close_the_list_of_matcher_forms(self):
        reviewer = bundles.reviewer(TASK, "")["prompt"]
        for rule in SCOPE_RULES:
            self.assertIn(rule, reviewer)
            self.assertNotIn(rule, bundles.implementer(TASK)["prompt"])
        korin = bundles.korin(TASK, {"id": "inc_0000aaaa", "kind": "runaway"}, [], [])["prompt"]
        self.assertIn("A card that asks for a phrase matcher " + FIX_CARD_RULE, korin)


if __name__ == "__main__":
    unittest.main()
