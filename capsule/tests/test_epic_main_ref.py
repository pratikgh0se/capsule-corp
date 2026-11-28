"""L136 `epic_sync.advance_main` (local main follows origin/main) and L155 `lane_behind` (a held
clone far behind main shows in status). Temp git repos with a local origin only."""
import os
from unittest import mock

from capsule import epic_sync, lane_behind, statusview, workspace
from capsule.tests.test_epic_clean import EpicCleanCase
from capsule.tests.test_epic_sync import BRANCH, git

NOW = 1_800_000_000


def commit(repo, name, when=None):
    (repo / name).write_text(name)
    git(repo, "add", ".")
    env = ["-c", "user.name=t", "-c", "user.email=t@t"]
    with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": f"{when or NOW} +0000"}):
        git(repo, *env, "commit", "-qm", name)


class MainRef(EpicCleanCase):
    def local_main(self):
        return git(self.ws, "rev-parse", "refs/heads/main").strip()

    def fetch(self):
        workspace._git("fetch", "origin", "main", ws=self.ws)

    def setUp(self):
        super().setUp()
        if not epic_sync._git_or_none("rev-parse", "--verify", "-q", "refs/heads/main", ws=self.ws):
            git(self.ws, "branch", "main", "origin/main")

    def test_fast_forwards_main_after_fetch(self):
        commit(self.origin, "a.txt")
        self.fetch()
        self.assertTrue(epic_sync.advance_main(self.ws))
        self.assertEqual(self.local_main(), git(self.origin, "rev-parse", "main").strip())

    def test_diverged_main_is_left_alone(self):
        git(self.ws, "branch", "-f", "main", "HEAD")
        (self.ws / "x.txt").write_text("x")
        workspace.commit(self.ws, "local-only")
        git(self.ws, "branch", "-f", "main", "HEAD")
        mine = self.local_main()
        commit(self.origin, "a.txt")
        self.fetch()
        self.assertFalse(epic_sync.advance_main(self.ws))
        self.assertEqual(self.local_main(), mine)

    def test_checked_out_main_is_skipped_with_local_changes_kept(self):
        git(self.ws, "checkout", "-q", "main")
        (self.ws / "wip.txt").write_text("wip")
        before = self.local_main()
        commit(self.origin, "a.txt")
        self.fetch()
        self.assertFalse(epic_sync.advance_main(self.ws))
        self.assertEqual(self.local_main(), before)
        self.assertTrue((self.ws / "wip.txt").exists())

    def test_main_checked_out_in_a_linked_worktree_is_skipped(self):
        other = self.dir / "other-wt"
        git(self.ws, "worktree", "add", "-q", str(other), "main")
        before = self.local_main()
        commit(self.origin, "a.txt")
        self.fetch()
        self.assertFalse(epic_sync.advance_main(self.ws))
        self.assertEqual(self.local_main(), before)

    def test_idle_sync_moves_main(self):
        commit(self.origin, "a.txt")
        epic_sync.sync_idle(self.conn, {"epics": [BRANCH], "board": "b"}, self.root, now=NOW)
        self.assertEqual(self.local_main(), git(self.origin, "rev-parse", "main").strip())


class Behind(EpicCleanCase):
    def hold(self):
        self.new_task()    # a review card on the branch holds the clone
        (self.ws / "tracked.txt").write_text("wip\n")

    def sync(self, now=NOW):
        epic_sync.sync_idle(self.conn, {"epics": [BRANCH], "board": "b"}, self.root, now=now)

    def test_held_clone_far_behind_is_recorded_and_named_in_status(self):
        self.hold()
        for n in range(lane_behind.BEHIND_MAX + 1):
            commit(self.origin, f"f{n}.txt", when=NOW)
        self.sync()
        got = lane_behind.find(self.conn, NOW)
        self.assertEqual([(g["branch"], g["behind"]) for g in got],
                         [(BRANCH, lane_behind.BEHIND_MAX + 1)])
        what = statusview._lane_behind_problems(self.conn, "b", NOW)[0]["what"]
        self.assertIn(BRANCH, what)
        self.assertIn(str(lane_behind.BEHIND_MAX + 1), what)
        wip = git(self.ws, "status", "--porcelain").strip()
        self.assertEqual(wip, "M tracked.txt")   # the WIP is untouched

    def test_held_clone_slightly_behind_and_fresh_is_quiet(self):
        self.hold()
        commit(self.origin, "f.txt", when=NOW)
        self.sync()
        self.assertEqual(lane_behind.find(self.conn, NOW), [])

    def test_old_base_alone_is_recorded(self):
        self.hold()
        commit(self.origin, "f.txt", when=NOW - (lane_behind.BEHIND_HOURS + 1) * 3600)
        self.sync()
        hours = lane_behind.find(self.conn, NOW)[0]["hours"]
        self.assertEqual(hours, lane_behind.BEHIND_HOURS + 1)

    def test_record_clears_when_caught_up_and_goes_stale(self):
        lane_behind.note(self.conn, BRANCH, 30, 0, NOW)
        self.assertEqual(len(lane_behind.find(self.conn, NOW)), 1)
        self.assertEqual(lane_behind.find(self.conn, NOW + lane_behind.STALE_S + 1), [])
        lane_behind.note(self.conn, BRANCH, 0, 0, NOW)
        self.assertEqual(lane_behind.find(self.conn, NOW), [])

    def test_unheld_clone_is_synced_not_flagged(self):
        for n in range(lane_behind.BEHIND_MAX + 1):
            commit(self.origin, f"f{n}.txt", when=NOW)
        self.sync()
        self.assertEqual(lane_behind.find(self.conn, NOW), [])

    def test_held_check_leaves_origin_main_local_main_and_fetch_head_alone(self):
        self.hold()
        for n in range(lane_behind.BEHIND_MAX + 1):
            commit(self.origin, f"f{n}.txt")
        git(self.origin, "tag", "v1", "main")
        git(self.ws, "branch", "-f", "main", "HEAD")
        def refs():
            return (git(self.ws, "rev-parse", "origin/main").strip(),
                    git(self.ws, "rev-parse", "main").strip())
        before = refs()
        fetch_head = workspace.gitdir(self.ws) / "FETCH_HEAD"
        had = fetch_head.exists()
        self.sync()
        self.assertEqual(refs(), before)
        self.assertEqual(fetch_head.exists(), had)
        self.assertEqual(git(self.ws, "tag").strip(), "")
        self.assertEqual(len(lane_behind.find(self.conn, NOW)), 1)

    def test_held_check_is_throttled(self):
        self.hold()
        self.sync()
        for n in range(lane_behind.BEHIND_MAX + 1):
            commit(self.origin, f"f{n}.txt")
        self.sync(NOW + 60)
        self.assertEqual(lane_behind.find(self.conn, NOW + 60), [])
        self.sync(NOW + lane_behind.CHECK_S)
        self.assertEqual(len(lane_behind.find(self.conn, NOW + lane_behind.CHECK_S)), 1)

    def test_failed_fetch_keeps_the_old_record(self):
        self.hold()
        lane_behind.note(self.conn, BRANCH, 30, 0, NOW)
        with mock.patch.object(workspace, "_git", side_effect=RuntimeError("net down")):
            epic_sync._note_held(self.conn, BRANCH, self.ws, NOW + 1)
        self.assertEqual(lane_behind.find(self.conn, NOW + 1)[0]["behind"], 30)

    def test_record_is_cleared_by_sync_once_the_clone_is_unheld(self):
        lane_behind.note(self.conn, BRANCH, 30, 0, NOW)
        self.sync()   # no card holds the clone
        self.assertEqual(lane_behind.find(self.conn, NOW), [])
