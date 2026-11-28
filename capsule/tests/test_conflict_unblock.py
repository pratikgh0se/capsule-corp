"""t_425638cf (inc_2fd5958b): a human `unblock` of an `epic_sync_conflict` hold sends the card to
review, not to a new implementer run: the gates already passed and the work is committed. The
setup is `test_epic_sync.EpicSyncHook`'s: a real conflict through a real `Tick`."""
import unittest
from pathlib import Path

from capsule import blocks, db, epic_sync, paths, workspace
from capsule.tests.test_tick import BRANCH, GUIDE_OK, IMPL_DONE, REV_PASS, VAL_PASS, TickCase
from capsule.tests.test_tick import git as tick_git


class HandMergedConflict(TickCase):
    def setUp(self):
        super().setUp()
        origin = self.home / "origin"
        tick_git(origin, "branch", "main", BRANCH)
        tick_git(origin, "checkout", "-q", "main")
        (origin / "README.md").write_text("main changed it\n")
        tick_git(origin, "add", ".")
        tick_git(origin, "commit", "-qm", "main edits README")
        tick_git(origin, "checkout", "-q", BRANCH)
        self.script["implementer"] = [[IMPL_DONE]]
        self.hooks["implementer"] = lambda job, sp, run: (
            Path(job["workspace"]) / "README.md").write_text("the card changed it too\n")
        self.ws = paths.board_dir("test", self.home) / "work" / workspace._slug(BRANCH)
        self.run_ticks("blocked")

    def run_ticks(self, status, n=8):
        for _ in range(n):
            self.tick.once()
            if self.task()["status"] == status:
                break

    def impl_runs(self):
        return self.conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=? AND "
                                 "profile='implementer'", (self.tid,)).fetchone()[0]

    def kinds(self):
        return [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=? "
                                                "ORDER BY id", (self.tid,))]

    def hand_merge(self):
        workspace._git("fetch", "origin", "main", ws=self.ws)
        with self.assertRaises(RuntimeError):
            workspace._git(*workspace.IDENTITY, "merge", "--no-edit", "origin/main", ws=self.ws)
        (self.ws / "README.md").write_text("main changed it\nthe card changed it too\n")
        workspace._git("add", "-A", ws=self.ws)
        workspace._git(*workspace.IDENTITY, "commit", "-qm", "merge main by hand", ws=self.ws)

    def settle(self, rounds=2):
        for _ in range(rounds):
            self.script["validator"].append([VAL_PASS])
            self.script["reviewer"].append([REV_PASS])
            self.script["guide"].append([GUIDE_OK])
        self.run_ticks("done", 14)

    def test_an_unblock_after_a_hand_merge_goes_to_review_without_an_implementer_run(self):
        self.assertEqual(self.task()["block_kind"], epic_sync.CONFLICT_KIND)
        runs = self.impl_runs()
        self.hand_merge()
        self.assertTrue(blocks.unblock(self.conn, self.tid, by="human"))
        self.assertEqual(self.task()["status"], "review")
        self.settle()
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.impl_runs(), runs)
        tail = self.kinds()[self.kinds().index("unblocked"):]
        self.assertEqual(tail[:2], ["unblocked", "request_review"])
        self.assertNotIn("epic_synced", tail)    # the hand merge left a no-op merge for the sync
        row = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND "
                                "kind='request_review' ORDER BY id DESC LIMIT 1",
                                (self.tid,)).fetchone()
        self.assertIn('"by": "unblock"', row[0])
        self.assertIn("after_run", row[0])

    def test_an_unblock_with_no_hand_merge_goes_to_review_and_holds_again(self):
        runs = self.impl_runs()
        self.assertTrue(blocks.unblock(self.conn, self.tid, by="human"))
        self.assertEqual(self.task()["status"], "review")
        self.settle()
        self.assertEqual(self.task()["status"], "blocked")
        self.assertEqual(self.task()["block_kind"], epic_sync.CONFLICT_KIND)
        self.assertEqual(self.impl_runs(), runs)

    def test_a_human_unblock_of_a_needs_input_hold_still_starts_an_implementer_run(self):
        self.conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (self.tid,))
        self.assertTrue(blocks.unblock(self.conn, self.tid, by="human"))
        self.assertEqual(self.task()["status"], "ready")
        runs = self.impl_runs()
        self.script["implementer"].append([IMPL_DONE])
        self.tick.once()
        self.assertEqual(self.impl_runs(), runs + 1)

    def test_a_conflict_hold_with_an_open_parent_stays_on_the_todo_route(self):
        parent = db.new_task(self.conn, title="parent", status="ready")
        self.conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)",
                          (parent, self.tid))
        self.assertTrue(blocks.unblock(self.conn, self.tid, by="human"))
        self.assertEqual(self.task()["status"], "todo")

    def test_a_worker_unblock_of_a_conflict_hold_keeps_its_route(self):
        self.assertTrue(blocks.unblock(self.conn, self.tid, by="worker"))
        self.assertEqual(self.task()["status"], "ready")


if __name__ == "__main__":
    unittest.main()
