"""L424: the landing's `completed` event carries M115's acceptance line, from the card's latest
`blind_gated` event: green, red (K of N failing), or `acceptance: none` with no such event."""
import json

from capsule import landing
from capsule.db import event
from capsule.tests.test_landing_step import NOW, Step


class TestCompletedAcceptance(Step):
    def complete(self, sha):
        checks = [{"name": "gates", "status": "pass"}]
        landing._complete(self.conn, self.tid, sha, {"checks": checks}, NOW + 5)
        last = self.conn.execute("SELECT payload FROM task_events WHERE kind='completed' "
                                 "ORDER BY id DESC LIMIT 1").fetchone()[0]
        return json.loads(last)

    def gated(self, counts, failing):
        event(self.conn, self.tid, "blind_gated", {"counts": counts, "failing": failing, "leaks": 0,
                                                   "held": "", "detail": ""}, now=NOW + 2)

    def test_a_card_with_no_blind_run_records_acceptance_none(self):
        got = self.complete("a" * 40)
        self.assertEqual(got["acceptance"], "acceptance: none")
        self.assertEqual(got["gates"], {"gates": "pass"})

    def test_a_green_blind_run_records_acceptance_green(self):
        self.gated({"pass": 3, "fail": 0, "error": 0, "skipped": 0}, 0)
        self.assertEqual(self.complete("b" * 40)["acceptance"], "acceptance: green")

    def test_a_red_blind_run_records_k_of_n_failing(self):
        self.gated({"pass": 1, "fail": 1, "error": 1, "skipped": 0}, 2)
        self.assertEqual(self.complete("c" * 40)["acceptance"], "acceptance: red (2 of 3 failing)")

    def test_the_latest_blind_run_counts(self):
        self.gated({"pass": 0, "fail": 2, "error": 0, "skipped": 0}, 2)
        self.gated({"pass": 2, "fail": 0, "error": 0, "skipped": 0}, 0)
        self.assertEqual(self.complete("d" * 40)["acceptance"], "acceptance: green")

    def test_the_same_completion_is_not_recorded_twice(self):
        self.complete("e" * 40)
        self.complete("e" * 40)
        n = self.conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='completed' "
                              "AND payload LIKE '%eeee%'").fetchone()[0]
        self.assertEqual(n, 1)
