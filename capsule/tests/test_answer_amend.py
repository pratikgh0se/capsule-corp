"""inc_8b32f804: a CoS `answer` that names an identifier of the card body is a ruling the body
does not carry yet. The hold stays and the outbox asks for `amend` first."""
import json
import tempfile
from pathlib import Path

from capsule import amend, blocks, db, human, interventions
from capsule.tests.test_tick import TickCase

BODY = "Acceptance 1: add `recurrence_problems(test_id, checkout)` in capsule/incident_check.py."
ANSWER = ("The review flags _recurrence_problems as a second caller. Inline it into that "
          "caller.")


class AnswerAmend(TickCase):
    def _hold(self, body=BODY):
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, self.tid))
        with db.txn(self.conn):
            blocks.block(self.conn, self.tid, kind="needs_input", reason="which way?")
        self.runner.up = False

    def _stamp(self):
        return json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='intervention'"
            " ORDER BY id DESC LIMIT 1", (self.tid,)).fetchone()[0])["outcome"]

    def test_an_answer_naming_a_card_body_identifier_keeps_the_hold_and_asks_for_amend(self):
        self._hold()
        human.write(self.home, "test", self.tid, "answer", ANSWER, author="cos", now=1790000000)
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "needs_input"))
        self.assertEqual(self._stamp(), "amend_needed")
        lines = [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE thread_key=? AND body LIKE '%answer kept the hold%'",
            (self.tid,))]
        self.assertEqual(lines, [
            f"capsule: {self.tid} answer kept the hold: it names recurrence_problems from the "
            "card body: amend the body (python3 -m capsule human test "
            f"{self.tid} amend --body-file <path> '<reason>'), then unblock"])

    def test_an_answer_with_no_shared_identifier_releases_the_hold(self):
        self._hold()
        human.write(self.home, "test", self.tid, "answer", "yes, go on", author="cos",
                    now=1790000000)
        self.tick.once()
        self.assertNotEqual(self.task()["status"], "blocked")
        self.assertEqual(self._stamp(), "applied")

    def test_unblock_after_amend_releases(self):
        self._hold()
        human.write(self.home, "test", self.tid, "answer", ANSWER, author="cos", now=1790000000)
        self.tick.once()
        path = Path(tempfile.mkdtemp()) / "body.md"
        path.write_text("Acceptance 1: inline the check in its one caller.")
        amend.amend(self.home, "test", self.tid, body_file=path, reason="the ruling",
                    now=1790000001, backup_dir=Path(tempfile.mkdtemp()))
        human.write(self.home, "test", self.tid, "unblock", "amended", author="cos",
                    now=1790000002)
        self.tick.once()
        self.assertNotEqual(self.task()["status"], "blocked")


class AnswersAnchor(TickCase):
    def _shared(self, answer, body):
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, self.tid))
        return interventions._shared_ids(self.conn, self.tid, answer)

    def test_a_capitalised_tail_is_not_an_identifier(self):
        self.assertEqual(self._shared("see Foo_bar", "uses oo_bar here"), [])

    def test_the_same_identifier_is_shared(self):
        self.assertEqual(self._shared("fix_card", "the fix_card"), ["fix_card"])

    def test_a_leading_underscore_is_stripped(self):
        self.assertEqual(self._shared("_fix_card", "the fix_card"), ["fix_card"])

    def test_a_longer_token_does_not_share_its_tail(self):
        self.assertEqual(self._shared("xfix_card", "the fix_card"), [])

    def test_a_token_continuing_past_the_match_is_not_shared(self):
        self.assertEqual(self._shared("fix_cardX", "the fix_card"), [])
