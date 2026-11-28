"""M032 (row 126: "routing (soft 5, hard 10), never a planner refusal"): intake admits
every card and it lands where it always did (a non-epic card in triage). Over the files_park
ceiling (10,
`maintenance/budgets.py`, override `OPS_FILES_PARK`) it carries a `budget:` comment and one
`needs_input` outbox line offering `reset`; over files_route (5), a soft comment and no line.
Two oracles each: the card's status and comments, and the outbox."""
import os
from unittest import mock

from capsule import blocks, intake, intent
from capsule.tests.test_tick import BRANCH, TickCase

REASON = "budget: 11 files over the ceiling of 10: split it or make it an epic"
SOFT = "over the 5-file routing threshold"


def names(n):
    return [f"capsule/f{i}.py" for i in range(n)]


class _Cards(TickCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE tasks SET assignee=NULL WHERE id=?", (self.tid,))  # idle
        self.conn.commit()

    def file_card(self, file_set, epic=None):
        rid = intake.enqueue(self.home, "test", kind="card", fingerprint="card", title="sized card",
                             body="b", source_board="operator", source_task=None, epic=epic,
                             assignee="dev-frontend", file_set=file_set)
        intake.ingest(self.conn, self.home, "test", self.cfg, now=1)
        return intake.card_for(self.conn, rid)

    def comments(self, card):
        return [r[0] for r in self.conn.execute(
            "SELECT body FROM task_comments WHERE task_id=?", (card["id"],))]

    def lines(self, card):
        return [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE thread_key=?", (card["id"],))]

    def assert_over(self, card, reason):
        self.assertEqual((card["status"], card["block_kind"]), ("triage", None))
        self.assertEqual(self.comments(card), [reason])
        lines = self.lines(card)
        self.assertEqual(len(lines), 1)
        self.assertIn(reason, lines[0])
        self.assertIn(f"{card['id']} reset '<answer>'", lines[0])
        self.assertNotIn("unblock", lines[0])



class SizeCeiling(_Cards):
    def test_eleven_files_non_epic_stays_in_triage_with_the_comment_and_one_line(self):
        card = self.file_card(names(11))
        self.assert_over(card, REASON)
        self.assertFalse(blocks.unblock(self.conn, card["id"], by="human", now=2))   # as on main
        row = self.conn.execute("SELECT status FROM tasks WHERE id=?", (card["id"],)).fetchone()
        self.assertEqual(row[0], "triage")

    def test_six_files_gets_the_soft_comment_and_no_line(self):
        card = self.file_card(names(6))
        self.assertEqual(card["status"], "triage")
        self.assertEqual(len(self.comments(card)), 1)
        self.assertIn(SOFT, self.comments(card)[0])
        self.assertEqual(self.lines(card), [])

    def test_ten_files_gets_the_soft_comment_and_no_line(self):
        card = self.file_card(names(10))
        self.assertEqual(card["status"], "triage")
        self.assertIn(SOFT, self.comments(card)[0])
        self.assertEqual(self.lines(card), [])

    def test_five_files_gets_nothing(self):
        card = self.file_card(names(5))
        self.assertEqual((self.comments(card), self.lines(card)), ([], []))

    def test_eleven_files_on_a_declared_epic_lands_normally_with_no_comment(self):
        card = self.file_card(names(11), epic=BRANCH)
        self.assertEqual((card["status"], card["branch_name"]), ("ready", BRANCH))
        self.assertEqual(self.comments(card), [])
        self.assertEqual(len(self.lines(card)), 1)
        self.assertIn("filed by intake", self.lines(card)[0])

    def test_eleven_files_on_an_undeclared_epic_is_over_the_ceiling(self):
        self.assert_over(self.file_card(names(11), epic="epic/nope"), REASON)

    def test_ops_files_park_override_moves_the_ceiling(self):
        with mock.patch.dict(os.environ, {"OPS_FILES_PARK": "12"}):
            card = self.file_card(names(11))
        self.assertIn(SOFT, self.comments(card)[0])
        self.assertEqual(self.lines(card), [])
        with mock.patch.dict(os.environ, {"OPS_FILES_PARK": "3"}):
            self.assert_over(self.file_card(names(4)), REASON.replace("11", "4").replace("10", "3"))

    def test_blank_and_duplicate_entries_do_not_count(self):
        card = self.file_card(names(10) + ["", "capsule/f0.py"])
        self.assertIn(SOFT, self.comments(card)[0])
        self.assertEqual(self.lines(card), [])


class IntentExamples(_Cards):
    """contracts/intent/intake_size_ceiling.jsonl, replayed through the real intake.ingest (Z1)."""

    def test_every_example_holds_on_the_real_code(self):
        examples = intent.load("intake_size_ceiling")
        self.assertEqual(len(examples), 3)                # an absent file would pass vacuously
        for n, ex in enumerate(examples):
            with self.subTest(ex["id"]):
                doc, exp = ex["given"]["document"], ex["expect"]
                epics = ex["given"].get("inputs", {}).get("epics", self.cfg["epics"])
                cfg = dict(self.cfg, epics=epics)
                rid = intake.enqueue(self.home, "test", kind=doc["kind"], fingerprint="card",
                                     title=f"example {n}", body="b", source_board="operator",
                                     source_task=None, epic=doc["epic"], assignee="dev-frontend",
                                     file_set=doc["file_set"])
                intake.ingest(self.conn, self.home, "test", cfg, now=1)
                card = intake.card_for(self.conn, rid)
                self.assertEqual(card["status"], exp["status"])
                want = [] if exp["comment"] == "<empty>" else [exp["comment"]]
                self.assertEqual(self.comments(card), want)
                if "outbox_lines" in exp:
                    self.assertEqual(len(self.lines(card)), exp["outbox_lines"])
