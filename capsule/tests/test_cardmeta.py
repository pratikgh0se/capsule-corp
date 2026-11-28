"""`capsule.cardmeta.parse_builds` (row 492): a card's `builds: <repo>:<branch>` line."""
import unittest

from capsule import cardmeta


class ParseBuilds(unittest.TestCase):
    def test_no_builds_line_is_none(self):
        self.assertIsNone(cardmeta.parse_builds("just a plain card body\nwith no marker"))

    def test_splits_repo_and_branch_on_the_first_colon(self):
        self.assertEqual(cardmeta.parse_builds("builds: capsule-corp:capsule/human-assign"),
                         ("capsule-corp", "capsule/human-assign"))

    def test_last_builds_line_wins(self):
        text = "builds: repo:old\nmore notes\nbuilds: repo:new\n"
        self.assertEqual(cardmeta.parse_builds(text), ("repo", "new"))

    def test_no_colon_is_none(self):
        self.assertIsNone(cardmeta.parse_builds("builds: capsule-corp-only"))

    def test_empty_repo_or_branch_is_none(self):
        self.assertIsNone(cardmeta.parse_builds("builds: :branch-only"))
        self.assertIsNone(cardmeta.parse_builds("builds: repo-only:"))

    def test_none_text_is_none(self):
        self.assertIsNone(cardmeta.parse_builds(None))


if __name__ == "__main__":
    unittest.main()
