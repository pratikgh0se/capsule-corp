"""D225 slice 3: slack_answer provenance marks and crash repair (split from test_slack_answer).
Oracles: the asks/done record and the human files, as in test_slack_answer. No Slack, no network."""
import json
import unittest
from unittest import mock

from capsule import paths, slack_answer
from capsule.tests.test_human_concurrent import with_provenance_fields
from capsule.tests.test_slack_answer import BOARD, NOW, OWNER, SlackAnswerCase


class SlackAnswerRepair(SlackAnswerCase):
    def crash_after_done(self, key):
        """The process dies after the ask is closed into asks/done and before the human write."""
        with mock.patch.object(slack_answer, "_write_human", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.click(key, "yes")
        self.assertTrue(self.done(key).exists())
        self.assertEqual(self.human_files(), [])

    def test_slack_answer_is_marked_when_the_schema_has_the_fields(self):
        key = self.ask(self.triage)["key"]
        with with_provenance_fields():
            self.assertTrue(self.click(key, "yes").accepted)
        (doc,) = self.human_files()
        self.assertEqual((doc["via"], doc["ask"]), ("slack", key))

    def test_repair_writes_the_missing_human_file_once(self):
        key = self.ask(self.triage)["key"]
        self.crash_after_done(key)
        with with_provenance_fields():
            self.assertEqual(slack_answer.repair(self.home), [key])
            self.assertEqual(slack_answer.repair(self.home), [])
        (doc,) = self.human_files()
        self.assertEqual((doc["kind"], doc["at"], doc["ask"], doc["via"]),
                         ("reset", NOW, key, "slack"))
        self.assertEqual(doc["text"], "owner said yes to: Let a big job go ahead?")

    def test_repair_without_the_ask_field_matches_by_task_verb_text_and_second(self):
        key = self.ask(self.triage)["key"]
        self.crash_after_done(key)
        self.assertEqual(slack_answer.repair(self.home), [key])
        self.assertEqual(slack_answer.repair(self.home), [])
        self.assertEqual(len(self.human_files()), 1)

    def test_repair_sees_a_file_the_tick_already_archived(self):
        key = self.ask(self.triage)["key"]
        self.assertTrue(self.click(key, "yes").accepted)
        human_dir = paths.board_dir(BOARD, self.home) / "human"
        (human_dir / "done").mkdir()
        (human_dir / "1.json").rename(human_dir / "done" / "1.json")
        self.assertEqual(slack_answer.repair(self.home), [])
        self.assertEqual(list(human_dir.glob("*.json")), [])

    def test_repair_skips_a_completed_answer(self):
        key = self.ask(self.triage)["key"]
        self.assertTrue(self.click(key, "no").accepted)
        self.assertEqual(slack_answer.repair(self.home), [])
        self.assertEqual(len(self.human_files()), 1)

    def test_repair_leaves_open_asks_and_non_verb_records_alone(self):
        open_key = self.ask(self.triage)["key"]
        self.done("c" * 16).parent.mkdir(parents=True, exist_ok=True)
        base = json.loads((paths.board_dir(BOARD, self.home) / "asks" / f"{open_key}.json")
                          .read_text())
        for i, (verb, pick) in enumerate((("merge", "yes"), ("nothing", "yes"), ("park", "zzz"))):
            rec = dict(base, key=f"{i}" * 16, choice=pick, answered_at=NOW, via="button",
                       answered_by=OWNER)
            rec["choices"] = {"yes": {"verb": verb, "text": "t"}}
            self.done(rec["key"]).write_text(json.dumps(rec))
        self.done("e" * 16).write_text("{not json")
        self.assertEqual(slack_answer.repair(self.home), [])
        self.assertEqual(self.human_files(), [])
        self.assertTrue((paths.board_dir(BOARD, self.home) / "asks" / f"{open_key}.json").exists())

    def test_repair_never_raises_when_the_write_is_refused(self):
        key = self.ask(self.triage)["key"]
        self.crash_after_done(key)
        with mock.patch.object(slack_answer, "_write_human", side_effect=ValueError("no")):
            self.assertEqual(slack_answer.repair(self.home), [])
        self.assertEqual(self.human_files(), [])

    def test_repair_skips_a_stale_card_and_leaves_the_ask(self):
        key = self.ask(self.triage)["key"]
        self.crash_after_done(key)
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.triage,))
        self.conn.commit()
        self.assertEqual(slack_answer.repair(self.home), [])
        self.assertEqual(self.human_files(), [])
        self.assertTrue((paths.board_dir(BOARD, self.home) / "asks" / f"{key}.json").exists())

    def test_repair_needs_an_unclosed_slack_shaped_record(self):
        key = self.ask(self.triage)["key"]
        self.crash_after_done(key)
        good = json.loads(self.done(key).read_text())
        for change in ({"closed": True}, {"via": "cli"}, {"via": None}, {"answered_by": "bob"},
                       {"answered_by": 7}):
            self.done(key).write_text(json.dumps(dict(good, **change)))
            self.assertEqual(slack_answer.repair(self.home), [], change)
        for junk in ("[1, 2, 3]", '"str"', "7", "null"):
            self.done(key).write_text(junk)
            self.assertEqual(slack_answer.repair(self.home), [], junk)
        self.assertEqual(self.human_files(), [])
        self.done(key).write_text(json.dumps(good))
        self.assertEqual(slack_answer.repair(self.home), [key])

    def test_repair_drops_the_orphaned_open_ask_once_the_file_exists(self):
        key = self.ask(self.triage)["key"]
        self.crash_after_done(key)
        ask_path = paths.board_dir(BOARD, self.home) / "asks" / f"{key}.json"
        self.assertTrue(ask_path.exists())
        self.assertEqual(slack_answer.repair(self.home), [key])
        self.assertFalse(ask_path.exists())
        self.assertTrue(self.done(key).exists())
        self.assertEqual(slack_answer.repair(self.home), [])
        self.assertEqual(len(self.human_files()), 1)
        # the file already exists but the open ask survived (crash after the write)
        ask_path.write_text(self.done(key).read_text())
        self.assertEqual(slack_answer.repair(self.home), [])
        self.assertFalse(ask_path.exists())
        self.assertEqual(len(self.human_files()), 1)


if __name__ == "__main__":
    unittest.main()
