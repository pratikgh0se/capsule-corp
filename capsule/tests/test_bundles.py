# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   HarnessNoteNames.test_note_does_not_name_the_retired_runtime
import json
import unittest

from capsule.tests.private_names import retired_names

from capsule import blocks, bundles


class HarnessNote(unittest.TestCase):
    def test_an_optional_item_goes_in_notes(self):
        sentence = ("A card item marked optional that you did not do goes in `notes`, "
                    "never in `left_undone`.")
        self.assertIn(sentence, " ".join(bundles.HARNESS_NOTE.split()))


class HarnessNoteNames(unittest.TestCase):
    def test_note_does_not_name_the_retired_runtime(self):
        for name in retired_names():
            self.assertNotIn(name, bundles.HARNESS_NOTE.lower())
        self.assertIn("kanban_request_review", bundles.HARNESS_NOTE)


class OptionalItemCases:
    """Mixed into test_tick.Failures; the fixtures (`self.tick`, `self.script`) come from there."""
    OPTIONAL_BODY = "Implements §6.8, rows 417, 420.\n1. the screen\n4. (optional) fold modules\n"
    OPTIONAL_NOTE = "Item 4 (optional Z8 module fold) not done"

    def _optional_run(self, body, left_undone):
        from capsule.tests.test_tick import IMPL_DONE
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, self.tid))
        self.conn.commit()
        self.script["implementer"] = [[dict(IMPL_DONE, left_undone=left_undone)]]
        self.tick.once()
        self.tick.once()

    def _events(self, kind):
        return [json.loads(p) for (p,) in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (self.tid, kind))]

    def test_an_optional_card_item_left_undone_routes_to_review(self):
        self._optional_run(self.OPTIONAL_BODY, [self.OPTIONAL_NOTE])
        self.assertEqual(self.task()["status"], "review")
        self.assertEqual(self._events("blocked"), [])
        notes = self._events("implementer_note")
        self.assertEqual([n["kind"] for n in notes], ["optional_item_skipped"])
        handoff = json.loads(self.runs("implementer")[0]["handoff"])
        self.assertEqual(handoff["left_undone"], [self.OPTIONAL_NOTE])

    def test_an_optional_item_with_a_real_gap_still_holds(self):
        gap = "tests/test_x.py still needs an assertion"
        self._optional_run(self.OPTIONAL_BODY, [self.OPTIONAL_NOTE, gap])
        self.assertEqual(self.task()["block_kind"], "needs_input")
        reason = self._events("blocked")[0]["reason"]
        self.assertIn(gap, reason)
        self.assertNotIn("Item 4", reason)

    def test_optional_in_the_line_but_not_on_the_card_still_holds(self):
        body = "Implements §6.8.\n1. the screen\n4. fold modules\n"
        self.assertFalse(blocks.is_optional_item_note(self.OPTIONAL_NOTE, body))
        self._optional_run(body, [self.OPTIONAL_NOTE])
        self.assertEqual(self.task()["block_kind"], "needs_input")
        self.assertEqual(self._events("implementer_note"), [])


if __name__ == "__main__":
    unittest.main()
