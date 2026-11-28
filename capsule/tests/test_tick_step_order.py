"""Row 509 size gate: `Tick.once`'s try-body (steps (1)-(6c)) moved into `_step_pass`, called
between config load and the flush/heartbeat/lookout tail (7)-(8). Two things this checks,
neither exercised by test_tick.py/test_tick_metrics.py: `_step_pass` runs before `_heartbeat`
before `_lookout` (a reordering of those three calls in `once()` is caught here even though it
passes the rest of the tick suite); and `_step_pass` still sits inside `once()`'s `try` - a
step that raises still reaches the flush/heartbeat/lookout `finally` chain, with the exception
itself propagating out of `once()` once that chain has run (moving `_step_pass` outside the
`try` would still pass every other tick test, since none of them makes a step raise). A new
file (not test_tick.py, whose file_length is already a frozen baseline entry - a test file
over that budget must not grow it further)."""
import unittest

from capsule.tests.test_tick import TickCase


class StepOrder(TickCase):
    def test_once_runs_its_steps_in_the_documented_order(self):
        order = []
        for name in ("_step_pass", "_heartbeat", "_lookout"):
            orig = getattr(self.tick, name)

            def wrapped(*a, _name=name, _orig=orig, **kw):
                order.append(_name)
                return _orig(*a, **kw)

            setattr(self.tick, name, wrapped)
        self.tick.once()
        self.assertEqual(order, ["_step_pass", "_heartbeat", "_lookout"])

    def test_a_failed_step_pass_still_runs_heartbeat_and_lookout(self):
        def boom(*a, **kw):
            raise RuntimeError("boom")
        self.tick._step_pass = boom

        ran = []
        for name in ("_heartbeat", "_lookout"):
            orig = getattr(self.tick, name)

            def wrapped(*a, _name=name, _orig=orig, **kw):
                ran.append(_name)
                return _orig(*a, **kw)

            setattr(self.tick, name, wrapped)

        with self.assertRaises(RuntimeError):
            self.tick.once()
        self.assertEqual(ran, ["_heartbeat", "_lookout"])


if __name__ == "__main__":
    unittest.main()
