"""Incident inc_3af85063: a merge conflict on a commit that main already holds (the lander replays
lane commits onto main as new commits, then main edits the same lines) settles with main's lines.
Every other conflict still holds. Subclasses only; the base module is imported, not its cases."""
import json
import shutil
import subprocess
from unittest import mock

from capsule import epic_sync, workspace
from capsule.tests import test_epic_sync as base

ID = ("-c", "user.name=t", "-c", "user.email=t@t")


def origin_commit(case, files, msg):
    for name, text in files.items():
        if text is None:
            base.git(case.origin, "rm", "-q", name)
        else:
            (case.origin / name).write_text(text)
    base.git(case.origin, "add", "-A")
    base.git(case.origin, *ID, "commit", "-qm", msg)


def lane_commit(case, files, msg="lane work"):
    for name, text in files.items():
        (case.ws / name).write_text(text)
    workspace._git(*workspace.IDENTITY, "add", "-A", ws=case.ws)
    workspace._git(*workspace.IDENTITY, "commit", "-qm", msg, ws=case.ws)


def replay_then_edit(case):
    """The lane holds commit C; main holds the same patch as a new commit, then edits it again."""
    lane_commit(case, {"README.md": "card\n"})
    origin_commit(case, {"README.md": "card\n"}, "replayed copy of the lane commit")
    origin_commit(case, {"README.md": "main later\n"}, "main edits the same lines")


class ReplayedConflict(base.EpicSyncCase):
    def row(self, task):
        return self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?",
                                 (task["id"],)).fetchone()

    def assert_held(self, task, before):
        self.assertEqual(self.row(task)["block_kind"], epic_sync.CONFLICT_KIND)
        self.assertEqual(workspace.head(self.ws), before)
        self.assertFalse((workspace.gitdir(self.ws) / "MERGE_HEAD").exists())
        self.assertEqual(workspace._git("status", "--porcelain", ws=self.ws).stdout.strip(), "")

    def run_sync(self):
        task = self.new_task()
        ok = epic_sync.after_gate_pass(self.conn, self.cfg, task, self.ws, run_id="r1", now=1)
        return task, ok

    def test_a_conflict_on_a_commit_main_already_holds_merges_and_keeps_mains_lines(self):
        replay_then_edit(self)
        task, ok = self.run_sync()
        self.assertTrue(ok)
        parents = workspace._git("log", "-1", "--format=%P", ws=self.ws).stdout.split()
        self.assertIn(base.git(self.origin, "rev-parse", "main").strip(), parents)
        self.assertEqual((self.ws / "README.md").read_text(), "main later\n")
        self.assertNotEqual(self.row(task)["status"], "blocked")
        self.assertFalse((workspace.gitdir(self.ws) / "MERGE_HEAD").exists())
        self.assertEqual(workspace._git("status", "--porcelain", ws=self.ws).stdout.strip(), "")

    def test_the_synced_event_names_the_files_main_kept(self):
        replay_then_edit(self)
        task, _ = self.run_sync()
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='epic_synced'",
            (task["id"],)).fetchone()["payload"])
        self.assertEqual(payload["main_kept"], ["README.md"])

    def test_a_lane_commit_with_no_patch_id_on_main_holds(self):
        lane_commit(self, {"README.md": "card\n"})
        origin_commit(self, {"README.md": "main later\n"}, "main edits README")
        before = workspace.head(self.ws)
        task, ok = self.run_sync()
        self.assertFalse(ok)
        self.assert_held(task, before)

    def test_a_settled_file_beside_a_real_conflict_settles_nothing(self):
        lane_commit(self, {"README.md": "card\n"}, "C1")
        lane_commit(self, {"other.txt": "real lane\n"}, "C2")
        origin_commit(self, {"README.md": "card\n"}, "replayed C1")
        origin_commit(self, {"README.md": "main later\n"}, "main edits README")
        origin_commit(self, {"other.txt": "real main\n"}, "main edits other.txt, no replay")
        before = workspace.head(self.ws)
        task, ok = self.run_sync()
        self.assertFalse(ok)
        self.assert_held(task, before)

    def test_a_revert_on_main_holds_even_when_the_patch_id_matches(self):
        replay_then_edit(self)
        origin_commit(self, {"unrelated.txt": "x\n"}, 'Revert "x"\n\nThis reverts commit abc123.')
        before = workspace.head(self.ws)
        task, ok = self.run_sync()
        self.assertFalse(ok)
        self.assert_held(task, before)

    def test_a_file_main_deleted_holds(self):
        lane_commit(self, {"README.md": "card\n"})
        origin_commit(self, {"README.md": "card\n"}, "replayed copy")
        origin_commit(self, {"README.md": None}, "main deletes README")
        before = workspace.head(self.ws)
        task, ok = self.run_sync()
        self.assertFalse(ok)
        self.assert_held(task, before)

    def test_a_lane_merge_commit_that_changes_the_file_holds(self):
        def git(*a):
            return workspace._git(*workspace.IDENTITY, *a, ws=self.ws)
        lane_commit(self, {"README.md": "card\n"}, "C")
        main_line = git("rev-parse", "HEAD").stdout.strip()
        git("checkout", "-q", "-b", "side", "HEAD~1")
        lane_commit(self, {"side.txt": "s\n"}, "side work")
        git("checkout", "-q", "-")
        git("merge", "--no-ff", "--no-commit", "side")
        (self.ws / "README.md").write_text("evil merge\n")
        git("add", "-A")
        git("commit", "-qm", "merge that changes README")
        self.assertEqual(git("rev-parse", "HEAD^1").stdout.strip(), main_line)
        origin_commit(self, {"README.md": "card\n"}, "replayed C")
        origin_commit(self, {"README.md": "main later\n"}, "main edits the same lines")
        before = workspace.head(self.ws)
        task, ok = self.run_sync()
        self.assertFalse(ok)
        self.assert_held(task, before)

    def test_a_planted_dot_git_in_the_clone_does_not_forge_the_patch_ids(self):
        replay_then_edit(self)
        dot = self.ws / ".git"
        if dot.is_dir() and not dot.is_symlink():
            shutil.rmtree(dot)
        else:
            dot.unlink()
        subprocess.run(["git", "init", "-q", str(self.ws)], check=True, capture_output=True)
        task, ok = self.run_sync()
        self.assertTrue(ok)
        self.assertEqual((self.ws / "README.md").read_text(), "main later\n")

    def test_a_git_failure_in_the_settle_step_leaves_no_merge_head(self):
        replay_then_edit(self)
        before = workspace.head(self.ws)
        real = workspace._git

        def flaky(*args, **kw):
            if args[:1] == ("checkout",):
                raise RuntimeError("boom")
            return real(*args, **kw)
        with mock.patch.object(workspace, "_git", side_effect=flaky):
            task, ok = self.run_sync()
        self.assertFalse(ok)
        self.assert_held(task, before)


class ReplayedIdle(base.EpicSyncCase):
    def test_idle_sync_settles_the_same_shape_and_publishes(self):
        ledger = f"epic:{base.BRANCH}"
        replay_then_edit(self)
        cfg = {"epics": [base.BRANCH], "board": "b"}
        epic_sync.sync_idle(self.conn, cfg, self.root, now=1_000_000)
        q = "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id"
        rows = self.conn.execute(q, (ledger,)).fetchall()
        self.assertEqual([r["kind"] for r in rows], ["epic_synced", "epic_published"])
        self.assertEqual((self.ws / "README.md").read_text(), "main later\n")
        published = base.git(self.origin, "rev-parse", base.BRANCH).strip()
        self.assertEqual(published, workspace.head(self.ws))
        self.assertEqual(json.loads(rows[0]["payload"])["main_kept"], ["README.md"])
