"""Loophole triage 2026-10-27, guide:2: the guide's `rows` must be among the rows it was handed,
for an empty bundle and for a non-empty one (row 468: no vault search). Z2: fails on the code
before `guide_notes.rows_not_handed` existed and the tick called it."""
import json
import unittest

from capsule import guide_notes
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, TickCase

DRIFTED = {"schema": "alignment_verdict.v1", "verdict": "drifted", "rows": [999]}


class RowsMustBeHanded(unittest.TestCase):
    def test_a_row_beyond_a_non_empty_bundle_is_refused(self):
        found = guide_notes.rows_not_handed(DRIFTED, {"rows": [417]})
        self.assertEqual([f["id"] for f in found], ["rows-not-handed"])
        self.assertEqual(found[0]["source"], "row 468")
        self.assertIn("[999]", found[0]["differs"][0])
        self.assertIn("[417]", found[0]["differs"][0])

    def test_a_row_with_nothing_handed_is_refused(self):
        self.assertEqual(len(guide_notes.rows_not_handed(DRIFTED, {"rows": []})), 1)
        self.assertEqual(len(guide_notes.rows_not_handed(DRIFTED, {})), 1)

    def test_rows_inside_the_bundle_pass(self):
        inside = dict(DRIFTED, rows=[417])
        self.assertEqual(guide_notes.rows_not_handed(inside, {"rows": [417, 500]}), [])
        self.assertEqual(guide_notes.rows_not_handed(dict(DRIFTED, rows=[]), {"rows": []}), [])
        self.assertEqual(guide_notes.rows_not_handed({"verdict": "aligned"}, {"rows": []}), [])

    def test_an_aligned_verdict_holds_nothing_so_it_is_not_refused(self):
        aligned = dict(DRIFTED, verdict="aligned")
        self.assertEqual(guide_notes.rows_not_handed(aligned, {"rows": [417]}), [])


class TickRefusesAStrayRow(TickCase):
    def test_a_drifted_verdict_on_an_unhanded_row_is_a_contradiction_not_a_hold(self):
        self.script["implementer"] = [[IMPL_DONE]]
        stray = dict(GUIDE_OK, verdict="drifted", rows=[999], reason="changes an interface")
        self.script["guide"] = [[stray], [stray]]
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] in ("blocked", "triage"):
                break
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "review_infra"))
        payloads = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='contradiction'",
            (self.tid,))]
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            self.assertIn("rows-not-handed", {e["id"] for e in payload["examples"]})


if __name__ == "__main__":
    unittest.main()
