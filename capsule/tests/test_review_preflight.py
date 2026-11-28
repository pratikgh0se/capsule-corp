"""W6-2: a card whose clone holds a half-done hand merge (MERGE_HEAD, or a conflict-marker triple
in a changed file) is held as needs_input before `finish_gate_pass` commits anything. Real origin,
real clone; the clone's `git log` and the board's status are the second oracle."""
import time
import unittest
from types import SimpleNamespace

from capsule import blocks, db, epic_sync, lease, workspace
from capsule.tests.test_epic_sync import EpicSyncCase, git
from capsule.tests.test_hand_merge_landing import TICK

MARKERS = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> origin/main\n"


class ReviewPreflight(EpicSyncCase):
    def finish(self, task, start=None):
        impl = {"id": None, "run_id": "r1", "handoff": None}
        tick = SimpleNamespace(**{**vars(TICK), "_start_sha": lambda *a: start or "0" * 40})
        epic_sync.finish_gate_pass(tick, self.conn, self.cfg, task, self.ws, [], impl, {}, "r1",
                                   int(time.time()), False, [], ("validator", "reviewer"))

    def status(self, task):
        return self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?",
                                 (task["id"],)).fetchone()

    def reason(self, task):
        row = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND "
                                "kind='blocked'", (task["id"],)).fetchone()
        return row[0]

    def assert_held(self, task, head, *needles):
        self.assertEqual(tuple(self.status(task)), ("blocked", epic_sync.CONFLICT_KIND))
        self.assertEqual(workspace.head(self.ws), head)
        for needle in needles:
            self.assertIn(needle, self.reason(task))

    def conflict(self):
        (self.origin / "README.md").write_text("main line\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "moved")
        (self.ws / "README.md").write_text("card line\n")
        workspace.commit(self.ws, "card work")
        workspace._git("fetch", "origin", "main", ws=self.ws)
        with self.assertRaises(RuntimeError):
            workspace._git(*workspace.IDENTITY, "merge", "--no-edit", "origin/main", ws=self.ws)

    def test_a_half_done_hand_merge_is_held_before_review(self):
        task = self.new_task()
        self.conflict()
        head = workspace.head(self.ws)
        self.finish(task)
        self.assert_held(task, head, "MERGE_HEAD present")

    def test_markers_in_a_non_python_file_hold_and_name_the_file(self):
        task = self.new_task()
        (self.ws / "notes.txt").write_text("a\n")
        workspace.commit(self.ws, "notes")
        head = workspace.head(self.ws)
        (self.ws / "notes.txt").write_text(MARKERS)
        self.finish(task)
        self.assert_held(task, head, "notes.txt")

    def test_markers_committed_by_the_card_are_held(self):
        task = self.new_task()
        start = workspace.head(self.ws)
        (self.ws / "notes.txt").write_text(MARKERS)
        workspace.commit(self.ws, "hand merge, committed with markers")
        head = workspace.head(self.ws)
        self.finish(task, start)
        self.assert_held(task, head, "notes.txt")

    def test_markers_already_on_the_base_are_not_the_cards(self):
        start = workspace.head(self.ws)
        (self.origin / "notes.txt").write_text(MARKERS)
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "m")
        workspace._git("fetch", "origin", "main", ws=self.ws)
        workspace._git(*workspace.IDENTITY, "merge", "--no-edit", "origin/main", ws=self.ws)
        task = self.new_task()
        (self.ws / "new.txt").write_text("work\n")
        self.finish(task, start)
        self.assertEqual(self.status(task)["status"], "done")

    def test_an_earlier_cards_committed_markers_do_not_hold_a_later_card(self):
        (self.ws / "example.txt").write_text(MARKERS)
        workspace.commit(self.ws, "earlier card: docs example")
        task = self.new_task()
        start = workspace.head(self.ws)
        (self.ws / "new.txt").write_text("work\n")
        workspace.commit(self.ws, "this card, clean")
        self.finish(task, start)
        self.assertEqual(self.status(task)["status"], "done")

    def test_a_renamed_file_that_already_had_markers_is_not_held(self):
        (self.ws / "example.txt").write_text(MARKERS)
        workspace.commit(self.ws, "earlier card: docs example")
        task = self.new_task()
        start = workspace.head(self.ws)
        (self.ws / "example.txt").rename(self.ws / "moved.txt")
        workspace.commit(self.ws, "this card renames it")
        self.finish(task, start)
        self.assertEqual(self.status(task)["status"], "done")

    def test_an_unknown_start_sha_fails_open_without_a_crash(self):
        task = self.new_task()
        (self.ws / "notes.txt").write_text(MARKERS)
        workspace.commit(self.ws, "markers, but no usable start")
        self.assertEqual(epic_sync._committed_markers(self.ws, "f" * 40), [])
        self.assertEqual(epic_sync._committed_markers(self.ws, None), [])
        self.assertIsNone(epic_sync.hold_half_merge(self.conn, task, self.ws, None, 5,
                                                    lambda *a: {}["no start_sha"]))
        self.assertEqual(self.status(task)["status"], "review")

    def test_a_clean_clone_proceeds_unchanged(self):
        task = self.new_task()
        (self.ws / "new.txt").write_text("work\n")
        self.finish(task)
        self.assertEqual(self.status(task)["status"], "done")

    def test_a_lone_separator_line_does_not_trigger(self):
        task = self.new_task()
        (self.ws / "README.md").write_text("Title\n=======\n\n| a | b |\n=======\n")
        self.finish(task)
        self.assertEqual(self.status(task)["status"], "done")

    def test_a_symlink_to_a_device_is_skipped_without_reading_it(self):
        link = self.ws / "zero"
        link.symlink_to("/dev/zero")
        self.assertFalse(epic_sync._has_markers(link))
        self.assertFalse(epic_sync._has_markers("/dev/zero"))

    def test_a_marker_triple_past_the_read_cap_is_not_read(self):
        big = self.ws / "big.txt"
        big.write_text("x" * epic_sync.MARKER_READ + "\n" + MARKERS)
        self.assertFalse(epic_sync._has_markers(big))
        big.write_text("x" * 100 + "\n" + MARKERS)
        self.assertTrue(epic_sync._has_markers(big))

    def test_a_non_ascii_file_name_with_markers_is_still_caught(self):
        task = self.new_task()
        name = "café.txt"
        (self.ws / name).write_text("a\n")
        workspace.commit(self.ws, "cafe")
        head = workspace.head(self.ws)
        (self.ws / name).write_text(MARKERS)
        self.finish(task)
        self.assert_held(task, head, name)

    def test_the_hold_names_the_clone_and_an_unblock_goes_to_review(self):
        task = self.new_task()
        self.conflict()
        self.finish(task)
        self.assertEqual(tuple(self.status(task)), ("blocked", epic_sync.CONFLICT_KIND))
        self.assertIn(str(self.ws), self.reason(task))
        self.assertIn("Finish the merge", self.reason(task))
        with db.txn(self.conn):
            lease.start_run(self.conn, task["id"], role="implementer", claim_lock="h:1",
                            expires=0, run_id="r1", now=1)
        self.assertTrue(blocks.unblock(self.conn, task["id"], by="human"))
        self.assertEqual(self.status(task)["status"], "review")

    def test_the_review_start_hold_leaves_no_run_behind(self):
        task = self.new_task()
        self.conflict()
        reason = epic_sync.hold_half_merge(self.conn, task, self.ws, None, 5)
        self.assertIn("MERGE_HEAD present", reason)
        self.assertEqual(self.status(task)["block_kind"], epic_sync.CONFLICT_KIND)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 0)

    def test_a_missing_clone_dir_is_no_hold_and_no_exception(self):
        task = self.new_task()
        self.assertIsNone(epic_sync.hold_half_merge(self.conn, task, "/no/such/clone", None, 5))


if __name__ == "__main__":
    unittest.main()
