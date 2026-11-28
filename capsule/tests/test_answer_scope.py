"""inc_8b4a0a4b: an `answer` that narrows a card is a ruling the card body does not carry, so
the writer refuses it and points to `amend`. `--no-lint` lets it through, marked in the text."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import db, human, paths, spool
from capsule.__main__ import main
from capsule.tests.test_human_author import session

NARROW = ("CoS scope note 2026-11-01: most of this card landed with t_be154c1b "
          "(drop_no_op_hunks in capsule/guide_notes.py). Only the VERDICT_NOTE sentence telling "
          "the guide to omit the key is left. Do only that; do not re-implement the hunk "
          "dropping.")
NARROW_537 = ("CoS scope note 2026-11-01: the CoS unblock of a routed Korin fix card already "
              "opens no incident on main (7c3cee4f, KORIN_HOLD_SQL). Only the is_routine_nudge "
              "sweep agreement is left. Do only that.")
RULING_559 = ("CoS ruling 2026-11-02: the full card scope stands (all 6 acceptance criteria). "
              "The earlier CoS scope note is withdrawn. Follow the guide's fix list: (1) "
              "parse_hunks raises ValueError when find == replace; (4) drop_no_op_hunks and its "
              "tick call landed with t_be154c1b: cite their file:line anchors on main; add only "
              "what is missing; (5) cite the test ids you ran.")
ANSWER_693 = ("Z8 rule 2 flags _recurrence_problems because it has one caller. Inline it into "
              "that caller (same lines, no new helper, no second caller), so the module stays "
              "under its size cap and precheck exits 0. Don't add a caller to satisfy the rule.")
WITHDRAWN = "I withdraw the earlier note: restore all 6 acceptance criteria on the card."


class NarrowingAnswer(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        p = paths.board_db("b", self.home)
        db.create_board(p)
        conn = db.connect(p)
        self.addCleanup(conn.close)
        self.tid = db.new_task(conn, title="c")

    def files(self):
        d = spool.human_dir(self.home, "b")
        return sorted(d.glob("*.json")) if d.is_dir() else []

    def cli(self, kind, text, *extra):
        argv = ["human", "b", self.tid, kind, text, "--capsule-home", str(self.home), *extra]
        with session(), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_a_narrowing_answer_is_refused_and_writes_no_human_file(self):
        with self.assertRaises(ValueError) as caught:
            human.write(self.home, "b", self.tid, "answer", NARROW)
        self.assertIn("human amend --body-file", str(caught.exception))
        self.assertEqual(self.files(), [])

    def test_the_refusal_names_the_matched_phrase(self):
        with self.assertRaises(ValueError) as caught:
            human.write(self.home, "b", self.tid, "answer", NARROW)
        self.assertIn("('Do only')", str(caught.exception))

    def test_ordinary_words_are_not_refused_and_spaced_phrases_are(self):
        for text in ("commonly happens when X is left", "nothing elsewhere", "exchange only"):
            human.write(self.home, "b", self.tid, "answer", text)
        for text in ("Do\nonly that", "Do  only that", "change\tonly the line"):
            with self.assertRaises(ValueError, msg=text):
                human.write(self.home, "b", self.tid, "answer", text)

    def test_the_other_scope_note_text_is_refused(self):
        with self.assertRaises(ValueError):
            human.write(self.home, "b", self.tid, "answer", NARROW_537)
        self.assertEqual(self.files(), [])

    def test_the_cli_prints_the_refusal_and_returns_one(self):
        rc, out, err = self.cli("answer", NARROW)
        self.assertEqual((rc, out), (1, ""))
        lines = err.splitlines()
        self.assertEqual(lines[0], "recorded as cos (not a terminal);"
                                   " the owner types it in a terminal")
        self.assertTrue(lines[-1].startswith("capsule human: "), err)
        self.assertIn("human amend --body-file", err)
        self.assertEqual(self.files(), [])

    def test_no_lint_lets_the_answer_in_and_marks_the_text(self):
        rc, out, err = self.cli("answer", NARROW, "--no-lint")
        self.assertEqual(rc, 0, err)
        doc = json.loads(Path(out.strip()).read_text())
        self.assertEqual(doc["text"], "[scope lint skipped] " + NARROW)

    def test_a_withdrawing_answer_a_plain_answer_and_a_reset_pass(self):
        for kind, text in (("answer", WITHDRAWN), ("answer", RULING_559), ("answer", ANSWER_693),
                           ("answer", "yes, go on"),
                           ("reset", "Change nothing else")):
            rc, out, err = self.cli(kind, text)
            self.assertEqual(rc, 0, (kind, text, err))
            self.assertEqual(json.loads(Path(out.strip()).read_text())["text"], text)
