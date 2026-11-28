"""Run-metadata meter labels (lane, batch_id, review_correct) written by tick.py."""
import json

from capsule import human
from capsule.tests import test_tick as tt


class MeterLabels(tt.TickCase):
    def setUp(self):
        super().setUp()
        self.cfg["contract_commands"] = ["unit-tests"]
        self.write_cfg()
        self.fix = tt.db.new_task(self.conn, title="fix card", body="", branch=tt.BRANCH)
        self.script["implementer"] = [[tt.IMPL_DONE]]

    def meta(self, role):
        return json.loads(self.runs(role)[0]["metadata"])

    def test_an_epic_branch_run_records_lane_epic_and_batch_id_on_every_role(self):
        self.tick.once()
        self.tick.once()
        for role in ("implementer", "validator"):
            meta = self.meta(role)
            self.assertEqual((meta["lane"], meta["batch_id"]), ("epic", tt.BRANCH), role)

    def test_a_non_epic_branch_and_a_null_branch_are_lane_card_with_no_batch(self):
        for branch in ("feat/x", None):
            meta = tt.config.lane_meta(self.cfg, branch)
            self.assertEqual(meta, {"lane": "card", "batch_id": None})

    def test_review_correct_is_false_when_a_check_was_overridden(self):
        self.script["validator"] = [[tt.VAL_FAIL_ONE]]
        self.tick.once()
        self.tick.once()
        human.write(self.home, "test", self.tid, "override", "flaky suite, tracked in the fix card",
                    now=1, check="unit-tests", ttl="72h", fix_card=self.fix, issuer="fable")
        self.runner.up = False
        self.tick.once()
        self.assertIs(self.meta("implementer")["review_correct"], False)

    def test_review_correct_is_true_when_the_validator_document_has_no_override(self):
        self.settle()
        self.assertIs(self.meta("implementer")["review_correct"], True)

    def test_review_correct_is_null_when_the_round_has_no_validator_document(self):
        self.script["validator"] = [[]]
        self.settle()
        self.assertEqual(self.meta("implementer")["review_correct"], None)

    def settle(self, passes=8):
        for _ in range(passes):
            self.tick.once()
            if self.task()["status"] in ("done", "blocked", "triage"):
                break
