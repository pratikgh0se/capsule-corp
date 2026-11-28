"""Guide `document-wrong` rulings (inc_c4473fc2).

An implementer-role ruling resets the card and the rerun implementer bundle carries the reason.
A reviewer-role ruling reruns only the reviewer; the reason survives an invalid first rerun and
a sibling contradiction is routed after the rerun."""
import json
import time
import unittest

from capsule import db
from capsule.tests import test_tick
from capsule.tests.test_tick import (GUIDE_OK, IMPL_DONE, REV_CONTRADICTS, REV_PASS, VAL_PASS,
                                     ruling)

CONTRADICTION = {"role": "implementer", "examples": [{"id": "x"}], "precedence": "p",
                 "routed": "guide"}


class Helpers(test_tick.TickCase):
    def events(self, kind):
        return [json.loads(r[0] or "{}") for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (self.tid, kind))]

    def settle(self, passes=10):
        for _ in range(passes):
            self.tick.once()


class ImplementerDocumentWrong(Helpers):
    def test_document_wrong_resets_the_card_with_the_reason_for_the_rerun(self):
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        self.script["guide"] = [[GUIDE_OK]]
        self.tick.once()
        self.tick.once()
        impl, guide = self.runs("implementer")[0], self.runs("guide")[-1]
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='triage', block_kind='guide_ruling'"
                              " WHERE id=?", (self.tid,))
            db.event(self.conn, self.tid, "contradiction", CONTRADICTION,
                     run_pk=impl["id"], run_id=impl["run_id"])
        ev_id = self.conn.execute(
            "SELECT MAX(id) FROM task_events WHERE kind='contradiction'").fetchone()[0]
        self.tick._apply_ruling(self.conn, self.cfg, guide, ruling("document-wrong"),
                                ev_id, int(time.time()))
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual([e.get("by") for e in self.events("failure_reset")], ["guide"])
        self.tick.once()                                    # the implementer reruns
        rerun = self.runs("implementer")[-1]
        self.assertEqual(len(self.runs("implementer")), 2)
        prompt = self.bundle_prompt(rerun)
        self.assertIn("## Guide rulings", prompt)
        self.assertIn("(row 363)", prompt)


class ReviewerDocumentWrong(Helpers):
    def rule(self):
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[REV_CONTRADICTS]]
        self.script["guide"] = [[GUIDE_OK], [ruling("document-wrong")]]
        for _ in range(3):
            self.tick.once()

    def test_the_ruling_reaches_the_rerun_after_an_invalid_first_try(self):
        self.rule()
        self.script["reviewer"] = [[{"x": 1}], [REV_PASS]]
        self.script["validator"] = [[VAL_PASS]]
        self.settle()
        reviewers = self.runs("reviewer")
        self.assertEqual(len(self.runs("implementer")), 1)
        self.assertGreaterEqual(len(reviewers), 3)
        for run in reviewers[1:]:
            prompt = self.bundle_prompt(run)
            self.assertIn("## The guide ruled your last verdict wrong", prompt)
            self.assertIn("the fail is wrong (row 363)", prompt)
        self.assertNotIn("## The guide ruled", self.bundle_prompt(reviewers[0]))
        self.assertEqual(self.task()["status"], "done")

    def test_a_sibling_contradiction_is_routed_after_the_reviewer_reruns(self):
        self.rule()
        val = self.runs("validator")[-1]
        with db.txn(self.conn):
            db.event(self.conn, self.tid, "contradiction",
                     {"role": "validator", "precedence": "p",
                      "examples": [{"id": "v", "source": "s", "differs": ["d"]}],
                      "ruling_card": None, "routed": None},
                     run_pk=val["id"], run_id=val["run_id"])
        self.script["reviewer"] = [[REV_PASS]]
        self.tick.once()                                    # applies the ruling
        self.assertEqual(self.task()["status"], "review")
        self.assertEqual([(e["role"], e["routed"]) for e in self.events("contradiction")][-1],
                         ("validator", "pending"))
        self.settle(6)                                      # the rerun ends, the sibling routes
        self.assertEqual((self.task()["status"], self.task()["block_kind"]),
                         ("triage", "guide_ruling"))
        self.assertEqual(self.events("contradiction")[-1]["role"], "validator")


if __name__ == "__main__":
    unittest.main()
