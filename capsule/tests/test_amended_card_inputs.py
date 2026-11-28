"""inc_0ff71df2: a card body amendment retires the older rulings, and the rerun prompt says the
card text wins. Real tick over the stub runner; only names that exist on base are read."""
import json
import time
import unittest

from capsule import amend, db, runaway, spool
from capsule.tests import test_tick as tt

WINS = ("Where an earlier answer, ruling or finding in this prompt conflicts with the card text,"
        " the card text wins.")
OLD = "OLD-RULING-TEXT"
NEW_BODY = "Implements the amended card text, rows 417, 420.\n"


class RerunAfterAmendment(tt.TickCase):
    def setUp(self):
        super().setUp()
        self.script["implementer"] = [[tt.IMPL_DONE], [tt.IMPL_DONE]]
        self.script["reviewer"] = [[tt.REV_FAIL], [tt.REV_PASS]]
        self.script["validator"] = [[tt.VAL_PASS], [tt.VAL_PASS]]
        self.script["guide"] = [[tt.GUIDE_OK], [tt.GUIDE_OK]]

    def first_round(self):
        """Run one implementer round that the reviewer fails; the card waits in ready."""
        self.tick.once()
        self.tick.once()
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        self.runner.up = True

    def rule(self):
        db.event(self.conn, self.tid, "guide_ruling",
                 {"choice": "continue", "role": "runaway", "reason": OLD})
        self.conn.commit()

    def answer(self):
        hdir = spool.human_dir(self.home, "test")
        hdir.mkdir(parents=True, exist_ok=True)
        (hdir / "1.json").write_text(json.dumps(
            {"task_id": self.tid, "kind": "accept", "text": "OLD-ANSWER", "at": 1,
             "author": "cos"}))

    def amend_body(self, now, body=NEW_BODY, **kw):
        path = self.home / "body.md"
        path.write_text(body)
        amend.amend(self.home, "test", self.tid, body_file=path, reason="restate", now=now,
                    backup_dir=self.home / "bk", **kw)

    def rerun(self):
        for _ in range(4):
            self.tick.once()
            if len(self.runs("implementer")) == 2:
                break
        second = self.runs("implementer")[1]
        sp = self.spool_dir(second["run_id"])
        job = json.loads((sp / "job.json").read_text())
        return job["inputs"], json.loads((sp / "bundle.json").read_text())["prompt"]

    def test_a_ruling_older_than_a_body_amendment_does_not_reach_the_rerun(self):
        self.first_round()
        self.rule()
        self.answer()
        now = int(time.time()) + 10
        self.amend_body(now)
        self.assertEqual(runaway._body_amended_at(self.conn, self.tid), now)
        inputs, prompt = self.rerun()
        self.assertNotIn(OLD, json.dumps(inputs.get("guide_rulings") or []))
        self.assertNotIn(OLD, prompt)
        self.assertIn(WINS, prompt)
        self.assertIn("dead branch", prompt)         # the older finding stays
        self.assertIn("OLD-ANSWER", prompt)          # the older answer stays

    def test_a_ruling_recorded_after_the_amendment_reaches_the_rerun(self):
        self.first_round()
        self.amend_body(int(time.time()) + 10)
        self.rule()
        inputs, prompt = self.rerun()
        self.assertEqual(inputs.get("guide_rulings"), [OLD])
        self.assertIn(OLD, prompt)

    def test_an_amendment_of_the_file_set_alone_keeps_the_older_ruling(self):
        self.first_round()
        self.rule()
        amend.amend(self.home, "test", self.tid, file_set=["app.py"], reason="scope",
                    now=int(time.time()) + 10, backup_dir=self.home / "bk")
        inputs, prompt = self.rerun()
        self.assertEqual(inputs.get("guide_rulings"), [OLD])
        self.assertNotIn(WINS, prompt)

    def test_a_harness_amendment_keeps_the_ruling_and_adds_no_section(self):
        self.first_round()
        self.rule()
        db.event(self.conn, self.tid, "contract_amended",
                 {"issuer": "harness", "body_hash_before": "a", "body_hash_after": "b"},
                 now=int(time.time()) + 10)
        self.conn.commit()
        inputs, prompt = self.rerun()
        self.assertEqual(inputs.get("guide_rulings"), [OLD])
        self.assertNotIn(WINS, prompt)

    def test_an_amendment_older_than_the_latest_review_run_adds_no_section(self):
        self.amend_body(1)
        self.first_round()
        _, prompt = self.rerun()
        self.assertNotIn(WINS, prompt)


if __name__ == "__main__":
    unittest.main()
