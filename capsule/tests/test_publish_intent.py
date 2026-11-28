"""Replay of `contracts/intent/publish_ready.jsonl` (Zenkai Z2 second oracle for M043): each row's
`given` is driven through the real `capsule.publish` with the fake gh of test_publish_s2, and every
`expect` key is asserted. Skips until the CoS has stamped the file."""
import json
import unittest
from pathlib import Path

from capsule.tests import test_publish as base
from capsule.tests.test_publish_s2 import S2Case

ROWS = Path(__file__).resolve().parents[2] / "contracts" / "intent" / "publish_ready.jsonl"


def rows():
    return [json.loads(x) for x in ROWS.read_text().splitlines() if x.strip()]


class IntentExamples(S2Case):
    def moved(self, new_review):
        """A published, reviewed, ready PR; then the host branch moves to a new head."""
        self.review_file()
        self.write_want(all_done=True)
        self.run_publish()
        first = self.result()
        (self.fake / "argv.log").unlink()
        self.commit("two")
        if new_review:
            self.review_file("A fresh review of the new head.\n")
        self.write_want(all_done=True, cards=[{**base.CARDS[0], "title": "Renamed card"}])
        self.run_publish()
        return first, self.result()

    def assert_expect(self, key):
        if key == "pr_returned_to_draft":
            first, got = self.moved(True)
            self.assertNotEqual(first["head"], got["head"])
            self.assertEqual(self.names("pr", "ready")[0][2], "--undo")
        elif key == "stored_ready_and_review_cleared":
            _, got = self.moved(False)       # head moved, no fresh review: stays draft
            for k in ("ready", "reviewed", "reviewed_head"):
                self.assertNotIn(k, got)
        elif key == "new_head_reviewed_before_ready_again":
            _, got = self.moved(True)
            order = [c[:3] for c in self.calls()]
            ready = [i for i, c in enumerate(order) if c[:2] == ["pr", "ready"]]
            review = [i for i, c in enumerate(order) if c[:2] == ["pr", "review"]]
            self.assertEqual(order[ready[0]][2], "--undo")
            self.assertLess(ready[0], review[0])
            self.assertLess(review[0], ready[1])
            self.assertIn(f"capsule-review:7:{got['head'][:12]}:", self.reviews()[1])
            self.assertEqual(got["reviewed_head"], got["head"])
        elif key == "pr_body_lists_current_cards":
            self.moved(True)
            self.assertIn("t_aaaa0001: Renamed card", self.edits()[-1])
        else:
            self.fail(f"no replay for expect key {key}")

    def edits(self):
        return json.loads((self.fake / "edits.json").read_text())

    def test_every_row_replays(self):
        if not ROWS.exists():
            self.skipTest("contracts/intent/publish_ready.jsonl is not stamped yet")
        found = rows()
        self.assertTrue(found)
        for row in found:
            self.assertEqual(row["given"]["document"]["check"], "epic_pr_publish")
            for key, want in row["expect"].items():
                self.assertIs(want, True, key)
                with self.subTest(row=row["id"], expect=key):
                    self.setUp()                       # fresh remote, clone and fake gh per case
                    self.assert_expect(key)


if __name__ == "__main__":
    unittest.main()
