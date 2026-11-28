"""`epic_sync.sync_idle`: an idle epic clone picks up origin/main within one tick.
Temp git repos only."""
from unittest import mock

from capsule import epic_clean, epic_sync, workspace
from capsule.tests.test_epic_clean import EpicCleanCase
from capsule.tests.test_epic_sync import BRANCH, git

LEDGER = f"epic:{BRANCH}"


class SyncIdle(EpicCleanCase):
    def move_main(self, name="new.txt", text="main moved\n"):
        (self.origin / name).write_text(text)
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "moved")

    def run_sync(self):
        cfg = {"epics": [BRANCH], "board": "b"}
        epic_sync.sync_idle(self.conn, cfg, self.root, now=1_000_000)

    def kinds(self):
        q = "SELECT kind FROM task_events WHERE task_id=? ORDER BY id"
        return [r[0] for r in self.conn.execute(q, (LEDGER,))]

    def contains_main(self):
        args = ("merge-base", "--is-ancestor", "origin/main", "HEAD")
        return epic_sync._git_or_none(*args, ws=self.ws) is not None

    def host_has_clone_head(self):
        return git(self.origin, "rev-parse", BRANCH).strip() == workspace.head(self.ws)

    def test_idle_clone_gets_main_and_publishes(self):
        self.move_main()
        self.run_sync()
        self.assertTrue(self.contains_main())
        self.assertEqual(self.kinds(), ["epic_synced", "epic_published"])
        self.assertTrue(self.host_has_clone_head())

    def test_up_to_date_clone_is_a_no_op(self):
        self.run_sync()
        self.assertEqual(self.kinds(), [])

    def test_conflict_is_an_event_once_and_leaves_clone_clean(self):
        (self.ws / "tracked.txt").write_text("epic\n")
        workspace.commit(self.ws, "epic change")
        self.move_main("tracked.txt", "main\n")
        before = workspace.head(self.ws)
        self.run_sync()
        self.run_sync()
        self.assertEqual(self.kinds(), ["epic_sync_conflict"])
        self.assertEqual(workspace.head(self.ws), before)
        self.assertEqual(self.status(), "")

    def test_dirty_clone_is_not_merged(self):
        self.dirty()
        self.move_main()
        before = workspace.head(self.ws)
        with mock.patch.object(epic_clean, "_backup", side_effect=RuntimeError("no disk")):
            self.run_sync()   # the row 624 clean fails, so the dirty clone stays as it was
        self.assertEqual(self.kinds(), ["needs_input"])
        self.assertEqual(workspace.head(self.ws), before)

    def test_live_card_holds_the_clone(self):
        self.conn.execute("INSERT INTO tasks (id, title, status, branch_name, created_at) VALUES"
                          " ('t_live', 't', 'running', ?, 1)", (BRANCH,))
        self.move_main()
        before = workspace.head(self.ws)
        self.run_sync()
        self.assertEqual(workspace.head(self.ws), before)
        self.assertEqual(self.kinds(), [])

    def test_a_refused_push_is_retried_on_the_next_tick(self):
        """Merged but unpublished: main is now an ancestor of HEAD, so without a retry the
        publish was forgotten for good."""
        self.move_main()
        with mock.patch.object(epic_sync, "_free_host_branch", return_value="/host/wt"):
            self.run_sync()
            self.run_sync()      # still refused: no second event for the same state
        self.assertEqual(self.kinds(), ["epic_synced", "epic_push_refused"])
        self.assertFalse(self.host_has_clone_head())
        self.run_sync()          # the host worktree is clean now
        self.assertTrue(self.host_has_clone_head())
        self.assertEqual(self.kinds(), ["epic_synced", "epic_push_refused", "epic_published"])
        self.run_sync()          # published: nothing more to do
        self.assertEqual(len(self.kinds()), 3)

    def test_clean_step_merge_is_published_the_same_tick(self):
        """The clean merges main into a dirty clone; the sync then sees main already included,
        so publishing must come from HEAD being ahead of the host branch, not from a flag."""
        self.dirty()
        self.move_main()
        self.run_sync()
        self.assertTrue(self.contains_main())
        self.assertEqual(self.status(), "")
        self.assertTrue(self.host_has_clone_head())
        self.assertEqual(self.kinds(), ["epic_published"])

    def move_host_epic(self, name="host.txt"):
        git(self.origin, "checkout", "-q", BRANCH)
        self.move_main(name, "host moved\n")
        git(self.origin, "checkout", "-q", "main")

    def test_moved_host_branch_is_fast_forwarded_to_before_merging(self):
        self.move_host_epic()
        self.move_main()
        self.run_sync()
        self.assertTrue((self.ws / "host.txt").exists())
        self.assertTrue(self.contains_main())
        self.assertTrue(self.host_has_clone_head())
        self.assertEqual(self.kinds(), ["epic_synced", "epic_synced", "epic_published"])

    def test_diverged_host_branch_skips_the_idle_merge_without_holding_a_card(self):
        (self.ws / "mine.txt").write_text("epic\n")
        workspace.commit(self.ws, "epic change")
        self.move_host_epic()
        self.move_main()
        before = workspace.head(self.ws)
        self.run_sync()
        self.run_sync()
        self.assertEqual(self.kinds(), ["epic_sync_skipped"])
        self.assertEqual(workspace.head(self.ws), before)
        self.assertFalse(self.contains_main())
        held = self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE block_kind IS NOT NULL").fetchone()[0]
        self.assertEqual(held, 0)
