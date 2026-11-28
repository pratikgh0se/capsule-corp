"""The ruling stage lives in `capsule.tick_rulings` (a mixin) and `Tick` inherits it."""
import ast
import inspect
import unittest
from pathlib import Path

from capsule import tick

tick_rulings = None   # imported per test, so a tree without the module fails each test alone


class TickRulingsSplit(unittest.TestCase):
    def setUp(self):
        global tick_rulings
        from capsule import tick_rulings as module
        tick_rulings = module

    def test_tick_inherits_the_rulings_mixin(self):
        from capsule.tick_review import TickReview
        mro = tick.Tick.__mro__
        self.assertIn(tick_rulings.TickRulings, mro)
        self.assertLess(mro.index(TickReview),
                        mro.index(tick_rulings.TickRulings))
        for name, func in vars(tick_rulings.TickRulings).items():
            if not inspect.isfunction(func):
                continue
            self.assertNotIn(name, vars(tick.Tick))
            resolved = next(vars(c)[name] for c in tick.Tick.__mro__ if name in vars(c))
            self.assertIs(resolved, func)

    def test_the_mixin_never_imports_the_tick_module(self):
        tree = ast.parse(Path(tick_rulings.__file__).read_text())
        found = [n for n in ast.walk(tree)
                 if (isinstance(n, ast.ImportFrom) and (n.module or "").endswith("tick"))
                 or (isinstance(n, ast.Import) and any(a.name.endswith(".tick") for a in n.names))
                 or (isinstance(n, ast.ImportFrom) and any(a.name == "tick" for a in n.names))]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
