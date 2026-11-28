"""A run whose model has no prices.json row is warned about, never silently costed."""
import io
import sys
import unittest
from unittest import mock

from capsule import lookout_ship


def run(model):
    return "cap_runs", {"run_id": f"r-{model}", "model": model}


class UnpricedWarning(unittest.TestCase):
    def warn(self, lines):
        err, old = io.StringIO(), sys.stderr
        sys.stderr = err
        try:
            with mock.patch.object(lookout_ship.events, "log") as log:
                lookout_ship._warn_unpriced(lines)
        finally:
            sys.stderr = old
        return err.getvalue(), log

    def test_unknown_model_warns_on_stderr_and_logs_an_event(self):
        err, log = self.warn([run("claude-sonnet-5"), run("claude-nope-9"), run("claude-nope-9")])
        self.assertEqual(err.count("no price for model claude-nope-9"), 1)
        self.assertNotIn("claude-sonnet-5:", err)
        self.assertEqual(log.call_count, 1)
        self.assertEqual(log.call_args.kwargs["severity"], "warn")

    def test_priced_model_and_non_run_rows_stay_quiet(self):
        err, log = self.warn([run("claude-sonnet-5"), ("events", {"model": "claude-nope-9"})])
        self.assertEqual(err, "")
        log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
