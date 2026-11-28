"""Loophole triage 2026-10-27, A1 (HIGH): a git-lock mention glued to a real gap is not a git-lock
note, so the item is not stripped and the card still holds (row 445). Z2: the unit test and the
routing test both fail on the pre-fix `blocks.is_git_lock_note`."""
import unittest

from capsule import blocks
from capsule.tick import Tick

GLUED = "tests/test_server.py still needs a timeout assertion; git commit failed: index.lock"
LOCK = "git commit failed: index.lock"
IMPL = {"schema": "report.v1", "role": "implementer", "status": "done",
        "implemented": ["the server"],
        "left_undone": [], "commands": [{"cmd": "make test", "exit": 0}], "issues": [],
        "procedures_followed": ["tdd"]}


class GitLockNoteIsWholeOnly(unittest.TestCase):
    def test_a_real_gap_glued_to_a_lock_mention_is_not_a_note(self):
        self.assertFalse(blocks.is_git_lock_note(GLUED))
        self.assertFalse(blocks.is_git_lock_note(f"{LOCK}\nthe retry loop still needs a test"))

    def test_a_pure_lock_note_still_is(self):
        self.assertTrue(blocks.is_git_lock_note(f"{LOCK} exists; retried with git add"))

    def test_the_repro_keeps_its_item_and_routes_to_hold(self):
        for key in ("left_undone", "issues"):
            report = dict(IMPL, **{key: [GLUED, LOCK]})
            judged = Tick._drop_git_lock_notes(report)
            self.assertEqual(judged[key], [GLUED])
            self.assertEqual(Tick._route(judged), "hold")

    def test_a_pure_lock_note_alone_still_routes_to_review(self):
        judged = Tick._drop_git_lock_notes(dict(IMPL, issues=[LOCK]))
        self.assertEqual(Tick._route(judged), "review")


class GluedDodgesAreNotNotes(unittest.TestCase):
    """Opus re-review: a gap not worded like a known gap must still hold."""
    DODGES = ("the timeout assertion is absent", "did not write the retry handler",
              "could not finish the docs", "the server tests fail",
              "the CLI flag is unimplemented", "error handling is incomplete",
              "skipped the migration", "the timeout assertion was not written",
              "the git hook test is missing")

    def test_each_dodge_glued_to_a_lock_mention_is_not_a_note(self):
        for gap in self.DODGES:
            self.assertFalse(blocks.is_git_lock_note(f"{gap}; {LOCK}"), gap)
            self.assertFalse(blocks.is_git_lock_note(f"{LOCK}; {gap}"), gap)


class RealGitTextStaysANote(unittest.TestCase):
    """F1 (Opus review): the strict matcher judges a worker's report items only. A failed run's
    raw error is git's own stderr and keeps the any-match rule (t_892c388b)."""
    STDERR = ("fatal: Unable to create '/work/repo/.git/index.lock': File exists.\n\n"
              "Another git process seems to be running in this repository. Please make sure "
              "all processes are terminated then try again.")
    PURE = ("git commit failed: index.lock exists. Retried and it worked.",
            "Committed nothing. .git is read-only (EROFS)")

    def test_real_git_stderr_is_a_lock_mention_on_the_failure_path(self):
        self.assertTrue(blocks.mentions_git_lock(self.STDERR))
        self.assertFalse(blocks.mentions_git_lock("Another process seems to be running"))
        self.assertFalse(blocks.mentions_git_lock(""))

    def test_pure_notes_with_a_harmless_clause_are_still_notes(self):
        for text in self.PURE:
            self.assertTrue(blocks.mentions_git_lock(text), text)
            self.assertTrue(blocks.is_git_lock_note(text), text)


if __name__ == "__main__":
    unittest.main()
