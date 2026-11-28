"""The run mechanics live in `capsule.tick_runs` (a mixin) and `capsule.tick.Tick` inherits them."""
import ast
import unittest
from pathlib import Path

from capsule import tick

tick_runs = None   # imported per test, so a tree without the module fails each test alone

MOVED = ("_leases", "_alive", "_kill", "_is_our_worker", "_group_alive", "_apply_untracks",
         "_clone", "_claim_and_launch", "_launch_unspawned", "_spawn", "_run", "_live_run_ids",
         "_start_sha", "_paused", "_spool_root", "_work_root")


class TickRunsSplit(unittest.TestCase):
    def setUp(self):
        global tick_runs
        from capsule import tick_runs as module
        tick_runs = module

    def test_tick_inherits_the_runs_mixin(self):
        self.assertIn(tick_runs.TickRuns, tick.Tick.__mro__)
        for name in MOVED:
            self.assertIn(name, vars(tick_runs.TickRuns))
            self.assertNotIn(name, vars(tick.Tick))
            resolved = next(vars(c)[name] for c in tick.Tick.__mro__ if name in vars(c))
            self.assertIs(resolved, vars(tick_runs.TickRuns)[name])

    def test_the_mixin_never_imports_the_tick_module(self):
        tree = ast.parse(Path(tick_runs.__file__).read_text())
        found = [n for n in ast.walk(tree)
                 if (isinstance(n, ast.ImportFrom) and (n.module or "").endswith("tick"))
                 or (isinstance(n, ast.Import) and any(a.name.endswith(".tick") for a in n.names))
                 or (isinstance(n, ast.ImportFrom) and any(a.name == "tick" for a in n.names))]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
