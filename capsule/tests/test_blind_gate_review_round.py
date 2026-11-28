"""Blind tests landing for a card waiting in review (`gate.release`): the round's gate result was
computed before they landed ("no blind tests yet"), so it is discarded and a fresh validator and
contract round runs. No implementer round, no failure count, never a stale hold."""
import json
import unittest

from capsule import bundles, config, db
from capsule.blind import exempt, gate
from capsule.tests.test_blind_gate import SpoolCase


class TestReviewRound(SpoolCase):
    def held_in_review(self):
        self.held()                                  # in review; the round's gate said no tests
        self.conn.execute("UPDATE task_runs SET outcome='completed' WHERE run_id='impl1'")

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]

    def test_tests_landing_discard_the_stale_no_tests_round(self):
        self.held_in_review()
        self.assertIsNotNone(gate._round_validator(self.conn, self.card))
        runs = self.count()
        self.assertTrue(gate.release(self.conn, self.card, 7))
        self.assertIsNone(gate._round_validator(self.conn, self.card))   # a fresh round is owed
        self.assertEqual(self.status(), ("review", None, 0))             # not blocked, no strike
        self.assertEqual([e["by"] for e in self.kinds("request_review")], ["blind_landed"])
        self.assertEqual(self.kinds("blocked"), [])
        self.assertEqual(self.count(), runs)
        self.assertEqual(gate.inputs(self.conn, self.root / "spool", self.card, "reviewer"), {})
        self.assertFalse(gate.release(self.conn, self.card, 8))          # nothing stale left

    def test_the_stale_hold_is_never_applied_after_the_round_was_discarded(self):
        self.held_in_review()
        gate.release(self.conn, self.card, 7)
        self.assertIsNone(gate._round_validator(self.conn, self.card))   # nothing to judge
        self.assertEqual(self.status()[:2], ("review", None))

    def test_a_card_in_review_with_no_round_yet_is_left_alone(self):
        self.held_in_review()
        self.conn.execute("DELETE FROM task_runs WHERE profile='validator'")
        self.assertFalse(gate.release(self.conn, self.card, 7))
        self.assertEqual(self.kinds("request_review"), [])

    def contract(self, landed_first):
        """The round's contract job began (`contract_started` for `run1`) before the tests landed
        (`blind_landed`), or after."""
        order = ["blind_landed", "contract_started"] if landed_first else [
            "contract_started", "blind_landed"]
        for kind in order:
            db.event(self.conn, self.card, kind, {"run_id": "run1"})

    def judge(self, validator_run):
        """`gate.hold` as the tick calls it: `run_id` is the IMPLEMENTER's ('impl1'), the
        validator's own run id is `run1`."""
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.card,)).fetchone()
        cfg = {**self.cfg(), "redesign": {"blind_tests": "gate"}}
        return gate.hold(self.conn, cfg, self.root / "spool", task, validator_run, "impl1", 5)

    def test_a_contract_job_submitted_before_the_tests_landed_ends_in_a_fresh_round(self):
        self.held_in_review()
        self.contract(landed_first=False)
        self.assertTrue(self.judge(self.run))
        self.assertEqual(self.status(), ("review", None, 0))             # not held
        self.assertEqual(self.kinds("blocked"), [])
        self.assertEqual([e["by"] for e in self.kinds("request_review")], ["blind_landed"])
        self.assertIsNone(gate._round_validator(self.conn, self.card))

    def test_a_contract_job_submitted_after_the_tests_landed_is_still_a_hold(self):
        self.held_in_review()
        self.contract(landed_first=True)
        self.assertTrue(self.judge(self.run))
        self.assertEqual(self.status()[:2], ("blocked", "needs_input"))
        self.assertEqual(self.kinds("request_review"), [])

    def test_no_review_starts_while_a_tester_round_runs(self):
        self.held_in_review()
        cfg = {**self.cfg(), "redesign": {"blind_tests": "gate"}}
        self.assertEqual([t["id"] for t in exempt.reviewable(self.conn, cfg)], [self.card])
        body = self.conn.execute("SELECT body FROM tasks WHERE id=?", (self.card,)).fetchone()[0]
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, run_id, started_at, status, metadata)"
            " VALUES (?, ?, 'tester1', 3, 'running', ?)",
            (self.card, config.BLIND_TESTER, json.dumps({"card_hash": bundles.card_hash(body)})))
        self.assertEqual(exempt.reviewable(self.conn, cfg), [])


if __name__ == "__main__":
    unittest.main()
