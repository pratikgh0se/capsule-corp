"""M110 (verifier cap): a card held by `blind.gate.cap` shows its own label, not "spec stale"."""
import unittest

from capsule import blocks, lookout
from capsule.tests.test_statusview import Fixture

LABEL = "blind cap (2 verifier rounds failed)"


class CapLabel(Fixture):
    def held(self, *causes):
        """The card held by `blocks.block` once per cause, each hold after a re-review."""
        tid = self.task("Held", status="review")
        for n, cause in enumerate(causes, 1):
            self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
            blocks.block(self.conn, tid, kind="spec_stale", reason=f"r{n}", cause=cause, now=n)
        return tid

    def ask(self):
        (n,) = self.snap()["needs_you"]
        return n["ask"]

    def test_blind_cap_hold_shows_the_cap_label_in_status(self):
        self.held("blind_cap:abc123")
        self.assertIn(LABEL, self.ask())
        self.assertNotIn("spec it cites", self.ask())

    def test_real_spec_stale_hold_keeps_its_wording_in_status(self):
        self.held(None)
        self.assertEqual(self.ask(), "The spec it cites has changed; it needs your call.")

    def test_cap_hold_then_real_spec_stale_hold_says_spec_stale(self):
        self.held("blind_cap:abc", None)
        self.assertEqual(self.ask(), "The spec it cites has changed; it needs your call.")

    def test_real_spec_stale_hold_then_cap_hold_says_blind_cap(self):
        self.held(None, "blind_cap:abc")
        self.assertIn(LABEL, self.ask())

    def test_slack_notice_names_the_cap_not_the_raw_code(self):
        self.held("blind_cap:abc")
        rows = [str(tuple(r)) for r in self.conn.execute("SELECT * FROM outbox")]
        self.assertTrue(any(LABEL in r for r in rows), rows)
        self.assertFalse(any("(spec_stale)" in r for r in rows), rows)


class CapLabelLookout(unittest.TestCase):
    def test_blind_cap_hold_waits_with_the_cap_label(self):
        got = lookout.derive_waiting("spec_stale", "spec_stale", "blocked", False, None, None,
                                     cause="blind_cap:abc")
        self.assertEqual(got, ("owner", LABEL))

    def test_real_spec_stale_hold_still_says_spec_citation_is_stale(self):
        got = lookout.derive_waiting("spec_stale", "spec_stale", "blocked", False, None, None)
        self.assertEqual(got, ("owner", "spec citation is stale"))


if __name__ == "__main__":
    unittest.main()
