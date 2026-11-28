"""The review stage lives in `capsule.tick_review` (a mixin) and `capsule.tick` re-exports it."""
import ast
import inspect
import unittest
from pathlib import Path

from capsule import tick

tick_review = None   # imported per test, so a tree without the module fails each test alone


class TickReviewSplit(unittest.TestCase):
    def setUp(self):
        global tick_review
        from capsule import tick_review as module
        tick_review = module

    def test_tick_inherits_the_review_mixin(self):
        self.assertIn(tick_review.TickReview, tick.Tick.__mro__)
        for name, func in vars(tick_review.TickReview).items():
            if not inspect.isfunction(func):
                continue
            self.assertNotIn(name, vars(tick.Tick))
            resolved = next(vars(c)[name] for c in tick.Tick.__mro__ if name in vars(c))
            self.assertIs(resolved, func)

    def test_constants_are_re_exported_and_feed_readonly_roles(self):
        self.assertIs(tick.REVIEW_ROLES, tick_review.REVIEW_ROLES)
        self.assertEqual(tick.REVIEW_TRIES, tick_review.REVIEW_TRIES)
        self.assertTrue(set(tick_review.REVIEW_ROLES) <= set(tick.READONLY_ROLES))

    def test_the_mixin_never_imports_the_tick_module(self):
        tree = ast.parse(Path(tick_review.__file__).read_text())
        found = [n for n in ast.walk(tree)
                 if (isinstance(n, ast.ImportFrom) and (n.module or "").endswith("tick"))
                 or (isinstance(n, ast.Import) and any(a.name.endswith(".tick") for a in n.names))
                 or (isinstance(n, ast.ImportFrom) and any(a.name == "tick" for a in n.names))]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
