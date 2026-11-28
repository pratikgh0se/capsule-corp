"""t_3a5a7cc0: a validator `fail` that names no failing item is an invalid document (retry),
never a counted round. Own module so test_tick.py (frozen file length) does not grow."""
from capsule.tests.test_tick import IMPL_DONE, VAL_PASS, TickCase


class BareFail(TickCase):
    def test_a_bare_fail_verdict_is_a_retry_not_a_counted_round(self):
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["validator"] = [[dict(VAL_PASS, verdict="fail")]]
        for _ in range(3):
            self.tick.once()
        val = self.runs("validator")[0]
        self.assertEqual((val["status"], val["error_class"]), ("failed", "invalid_output"))
        self.assertIn("name the failing item", val["error"])
        self.assertEqual(self.task()["failure_count"], 0)
