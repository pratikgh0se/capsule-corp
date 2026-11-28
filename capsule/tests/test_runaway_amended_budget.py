"""inc_c8ab5bfc: the token, run and warn limits count from the later of the last human reset and
the last body amendment, so spend under a replaced contract does not trip the new one."""
from capsule import db, runaway
from capsule.tests.test_cost import BoardCase


class AmendedBudget(BoardCase):
    def setUp(self):
        super().setUp()
        self.tid = self.card(status="review")

    def check(self):
        return runaway.check(self.conn, {}, self.tid)

    def amend(self, before, after, now):
        db.event(self.conn, self.tid, "contract_amended",
                 {"body_hash_before": before, "body_hash_after": after}, now=now)

    def test_tokens_before_a_body_amendment_do_not_count_toward_the_limit(self):
        self.landed(*[200000] * 5)   # median 200000: the limit is 600000
        self.run_row(self.tid, tin=700000, start=10)
        self.amend("a", "b", 100)
        self.run_row(self.tid, tin=1, start=200)
        self.assertIsNone(self.check())

    def test_tokens_after_a_body_amendment_still_trip_at_the_limit(self):
        self.landed(*[200000] * 5)
        self.run_row(self.tid, tin=700000, start=10)
        self.amend("a", "b", 100)
        self.run_row(self.tid, tin=600000, start=200)
        self.assertEqual(self.check()[0], "tokens")

    def test_runs_before_a_body_amendment_do_not_count_toward_the_runs_limit(self):
        for _ in range(24):   # the floor of 8 runs makes the limit 24
            self.run_row(self.tid, start=10)
        self.assertEqual(self.check()[0], "runs")
        self.amend("a", "b", 100)
        self.run_row(self.tid, start=200)
        self.assertIsNone(self.check())

    def test_an_amendment_with_equal_body_hashes_refills_nothing(self):
        self.landed(*[200000] * 5)
        self.run_row(self.tid, tin=700000, start=10)
        self.amend("a", "a", 100)
        self.run_row(self.tid, tin=1, start=200)
        self.assertEqual(self.check()[0], "tokens")

    def test_a_harness_drift_amendment_refills_nothing(self):
        self.landed(*[200000] * 5)
        self.run_row(self.tid, tin=700000, start=10)
        db.event(self.conn, self.tid, "contract_amended",
                 {"issuer": "harness", "body_hash_before": "a", "body_hash_after": "b"}, now=100)
        self.run_row(self.tid, tin=1, start=200)
        self.assertEqual((self.check() or ("none",))[0], "tokens")

    def test_a_non_harness_amendment_still_refills(self):
        self.landed(*[200000] * 5)
        self.run_row(self.tid, tin=700000, start=10)
        db.event(self.conn, self.tid, "contract_amended",
                 {"issuer": "operator", "body_hash_before": "a", "body_hash_after": "b"}, now=100)
        self.run_row(self.tid, tin=1, start=200)
        self.assertIsNone(self.check())

    def test_a_bare_unblock_by_a_human_refills_nothing(self):
        self.landed(*[200000] * 5)
        self.run_row(self.tid, tin=700000, start=10)
        db.event(self.conn, self.tid, "unblocked", {"by": "human"}, now=100)
        self.run_row(self.tid, tin=1, start=200)
        self.assertEqual(self.check()[0], "tokens")
