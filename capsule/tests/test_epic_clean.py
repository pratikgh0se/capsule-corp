"""Row 624: `epic_clean.clean_idle` backs up and discards a dirty epic clone no card holds, then
merges `origin/main`. Two oracles per case: the function's effects (backup file, log) and the
clone's real git state read independently; temp git repos only, never the real boards."""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from capsule import db, epic_clean, incidents, lease, slack_digest, tick_runs, workspace
from capsule.tests import tmpclean
from capsule.tests.test_epic_sync import BRANCH, EpicSyncCase, git


class EpicCleanCase(EpicSyncCase):
    def setUp(self):
        super().setUp()
        (self.origin / "tracked.txt").write_text("one\n")
        (self.origin / "staged.txt").write_text("one\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "files")
        git(self.origin, "branch", "-f", BRANCH, "main")
        workspace.clone(str(self.origin), BRANCH, self.root)
        workspace._git("merge", "-q", "--ff-only", "origin/" + BRANCH, ws=self.ws)
        self.backups = self.dir / "epic-backups"

    def dirty(self):
        (self.ws / "tracked.txt").write_text("unstaged edit\n")
        (self.ws / "staged.txt").write_text("staged edit\n")
        workspace._git("add", "staged.txt", ws=self.ws)
        (self.ws / "fresh.txt").write_text("untracked\n")

    def status(self):
        return workspace._git("status", "--porcelain", ws=self.ws).stdout

    def run_clean(self):
        cfg = {"epics": [BRANCH], "board": "b"}
        epic_clean.clean_idle(self.conn, cfg, self.root, now=1_000_000)


class Cleaning(EpicCleanCase):
    def test_dirty_idle_clone_is_backed_up_discarded_and_synced(self):
        self.dirty()
        (self.origin / "new.txt").write_text("main moved\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "moved")
        self.run_clean()
        patches = list(self.backups.glob("*.patch"))
        self.assertEqual(len(patches), 1)
        self.assertEqual(self.status(), "")
        self.assertTrue((self.ws / "new.txt").exists())
        replay = self.dir / "replay"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(replay)], check=True)
        git(replay, "checkout", "-q", BRANCH)
        git(replay, "apply", str(patches[0]))
        self.assertEqual((replay / "tracked.txt").read_text(), "unstaged edit\n")
        self.assertEqual((replay / "staged.txt").read_text(), "staged edit\n")
        self.assertEqual((replay / "fresh.txt").read_text(), "untracked\n")

    def test_log_and_digest_entry(self):
        self.dirty()
        self.run_clean()
        row = self.conn.execute("SELECT body, channel FROM outbox WHERE body LIKE '%self-cleaned%'"
                                " OR body LIKE '%cleaned%'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["channel"], "file")
        note = self.conn.execute(
            "SELECT value FROM guard_state WHERE key LIKE 'epic_cleaned:%'").fetchone()
        self.assertIn("backup", json.loads(note[0])["text"])

    def test_the_morning_summary_names_the_cleanup(self):
        self.dirty()
        self.run_clean()
        lines = slack_digest._cleaned("b", self.conn, 0, 2_000_000)
        self.assertEqual(len(lines), 1)
        self.assertIn("cleaned", lines[0])

    def test_clean_clone_is_a_no_op(self):
        before = self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.run_clean()
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], before)


class GuardLaunch(EpicCleanCase):
    """Launch-time guard: a card never starts on, nor commits, work that is not its own."""

    def launch(self, tid):
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return tick_runs._guard_launch(self.conn, {"board": "b"}, row, self.ws, 5)

    def new(self):
        return db.new_task(self.conn, title="t", assignee="dev", branch=BRANCH, status="ready")

    def events(self, tid):
        q = "SELECT payload FROM task_events WHERE task_id=? AND kind='lane_leftover_set_aside'"
        return [json.loads(r[0]) for r in self.conn.execute(q, (tid,))]

    def test_leftover_no_card_owns_is_backed_up_applied_and_cleaned(self):
        self.dirty()
        tid = self.new()
        self.assertIsNone(self.launch(tid))
        self.assertEqual(self.status(), "")
        patch = next(self.backups.glob("*.patch"))
        git(self.ws, "apply", "--check", str(patch))
        self.assertEqual(self.events(tid), [{"owner": None, "backup": str(patch),
                                             "paths": self.events(tid)[0]["paths"]}])
        self.assertIn("fresh.txt", self.events(tid)[0]["paths"])

    def test_the_cards_own_dirt_is_untouched(self):
        tid = self.new()
        lease.start_run(self.conn, tid, role="implementer", claim_lock=None, expires=0,
                        run_id="r-own", now=1)
        self.dirty()
        before = self.status()
        self.assertIsNone(self.launch(tid))
        self.assertEqual(self.status(), before)
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.events(tid), [])

    def test_dirt_owned_by_another_card_is_set_aside_naming_it(self):
        other = db.new_task(self.conn, title="b", assignee="dev", branch=BRANCH, status="blocked")
        lease.start_run(self.conn, other, role="implementer", claim_lock=None, expires=0,
                        run_id="r-b", now=1)
        self.dirty()
        tid = self.new()
        self.assertIsNone(self.launch(tid))
        self.assertEqual(self.status(), "")
        self.assertEqual(self.events(tid)[0]["owner"], other)
        self.assertEqual(len(list(self.backups.glob("*.patch"))), 1)

    def test_a_failed_backup_refuses_and_leaves_the_files(self):
        self.dirty()
        elsewhere = self.dir / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.backups)
        before = self.status()
        tid = self.new()
        got = self.launch(tid)
        self.assertEqual(got[0], "worktree_dirty")
        self.assertIn("nothing discarded", got[1])
        self.assertEqual(self.status(), before)
        self.assertEqual(self.events(tid), [])

    def test_a_clean_tree_passes(self):
        self.assertIsNone(self.launch(self.new()))
        self.assertFalse(self.backups.exists())


    def test_a_path_with_a_space_is_named_whole(self):
        (self.ws / "my file.txt").write_text("x\n")
        tid = self.new()
        self.assertIsNone(self.launch(tid))
        self.assertIn("my file.txt", self.events(tid)[0]["paths"])
        self.assertNotIn("file.txt\"", self.events(tid)[0]["paths"].replace("my file.txt", ""))

    def test_an_unreadable_status_blocks_without_discarding(self):
        self.dirty()
        (workspace.gitdir(self.ws) / "index").write_bytes(b"corrupt")
        tid = self.new()
        got = self.launch(tid)
        self.assertEqual(got[0], "worktree_dirty")
        self.assertIn("could not read the lane clone status", got[1])
        self.assertTrue((self.ws / "fresh.txt").exists())

    def test_a_second_card_on_a_lane_with_a_failed_backup_gets_no_second_notice(self):
        self.dirty()
        self.backups.write_text("a file where the directory should be")
        first, second = self.new(), self.new()
        self.assertEqual(self.launch(first)[0], "worktree_dirty")
        def count():
            return self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        notices = count()
        got = self.launch(second)
        self.assertEqual(got[0], "worktree_dirty")
        self.assertEqual(count(), notices)
        self.assertTrue(epic_clean.backup_failed_open(self.conn, BRANCH))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)
        self.assertTrue((self.ws / "fresh.txt").exists())

class FailedBackupNotice(EpicCleanCase):
    def ticks(self, n):
        cfg = {"epics": [BRANCH], "board": "b"}
        for i in range(n):
            epic_clean.clean_idle(self.conn, cfg, self.root, now=1_000_000 + i)

    def rows(self, table):
        return self.conn.execute(f"SELECT * FROM {table}").fetchall()

    def test_repeated_ticks_send_one_notice_and_open_one_incident(self):
        (self.ws / "stray.txt").write_text("x\n")
        self.backups.write_text("a file where the directory should be")
        self.ticks(4)
        self.assertEqual(len(self.rows("outbox")), 1)
        self.assertEqual(len(self.rows("incidents")), 1)
        self.assertIn("stray.txt", self.rows("incidents")[0]["evidence"])
        self.assertIn("stray.txt", str(tuple(self.rows("outbox")[0])))
        self.assertTrue((self.ws / "stray.txt").exists())

    def test_an_open_incident_blocks_a_second_one_past_midnight_until_closed(self):
        (self.ws / "stray.txt").write_text("x\n")
        self.backups.write_text("a file where the directory should be")
        cfg = {"epics": [BRANCH], "board": "b"}
        day = 1_000_000
        epic_clean.clean_idle(self.conn, cfg, self.root, now=day)
        epic_clean.clean_idle(self.conn, cfg, self.root, now=day + 2 * 86400)
        self.assertEqual((len(self.rows("incidents")), len(self.rows("outbox"))), (1, 1))
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE status='open'",
                          (day,))
        epic_clean.clean_idle(self.conn, cfg, self.root, now=day + 3 * 86400)
        self.assertEqual(len(self.rows("incidents")), 2)
        notices = [r for r in self.rows("outbox")
                   if "clone is dirty, backup failed" in str(tuple(r))]
        self.assertEqual(len(notices), 2)      # the repeat page is a third, separate row

    def test_the_stray_list_is_capped(self):
        for i in range(13):
            (self.ws / f"f{i:02}.txt").write_text("x\n")
        got = epic_clean._stray(self.ws)
        self.assertIn("f09.txt", got)
        self.assertNotIn("f10.txt", got)
        self.assertTrue(got.endswith("+3 more"))


class Refusals(EpicCleanCase):
    def test_a_running_card_on_the_branch_keeps_its_clone(self):
        self.dirty()
        db.new_task(self.conn, title="t", assignee="dev", branch=BRANCH, status="running")
        before = self.status()
        self.run_clean()
        self.assertEqual(self.status(), before)
        self.assertFalse(self.backups.exists())

    def test_a_card_on_another_branch_does_not_hold_this_clone(self):
        self.dirty()
        db.new_task(self.conn, title="t", assignee="dev", branch="epic/other", status="running")
        self.run_clean()
        self.assertEqual(self.status(), "")

    def locked(self, expires):
        tid = self.card("ready", ran=False)
        self.conn.execute("UPDATE tasks SET claim_lock=?, claim_expires=? WHERE id=?",
                          ("host:r1", expires, tid))
        return tid

    def test_a_fresh_claim_lock_holds_the_lane(self):
        tid = self.locked(2000)
        self.assertEqual(epic_clean.holders(self.conn, BRANCH, now=1000), {tid})

    def test_a_stale_claim_lock_does_not_hold_the_lane(self):
        self.locked(999)
        self.assertEqual(epic_clean.holders(self.conn, BRANCH, now=1000), set())
        self.assertEqual(epic_clean.lane_holds(self.conn, 1000), [])

    def test_a_claim_lock_without_expiry_fails_closed(self):
        tid = self.locked(None)
        self.assertEqual(epic_clean.holders(self.conn, BRANCH, now=1000), {tid})

    def test_a_stale_claim_lock_is_not_live_in_lane_holds(self):
        tid = self.locked(999)
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        self.conn.execute("UPDATE tasks SET claim_expires=2000 WHERE id=?", (tid,))
        self.assertTrue(epic_clean.lane_holds(self.conn, 1000)[0]["live"])
        self.assertEqual(epic_clean.lane_holds(self.conn, 3000), [])

    def card(self, status, ran=True):
        tid = db.new_task(self.conn, title="t", assignee="dev", branch=BRANCH, status=status)
        if ran:
            lease.start_run(self.conn, tid, role="implementer", claim_lock=None, expires=0,
                            run_id="r-" + tid, now=1)
        return tid

    def test_a_returned_card_that_ran_keeps_its_uncommitted_work(self):
        for status in ("ready", "blocked", "triage"):
            self.card(status)
        self.dirty()
        before = self.status()
        self.run_clean()
        self.assertEqual(self.status(), before)
        self.assertFalse(self.backups.exists())

    def run_at(self, tid, at):
        lease.start_run(self.conn, tid, role="implementer", claim_lock=None, expires=0,
                        run_id=f"r-{tid}-{at}", now=at)

    def test_old_blocked_cards_do_not_hold_when_a_done_card_ran_after_them(self):
        for at in (1, 2, 3):
            self.run_at(self.card("blocked", ran=False), at)
        self.run_at(self.card("done", ran=False), 9)
        self.dirty()
        self.run_clean()
        self.assertEqual(self.status(), "")

    def test_run_order_is_by_id_not_started_at(self):
        self.run_at(self.card("blocked", ran=False), 900)    # earlier id, later clock
        self.run_at(self.card("done", ran=False), 5)
        self.assertFalse(epic_clean.held(self.conn, BRANCH))
        self.run_at(self.card("blocked", ran=False), 1)      # later id, earlier clock
        self.assertTrue(epic_clean.held(self.conn, BRANCH))

    def test_no_completion_ever_means_any_card_with_a_run_holds(self):
        self.run_at(self.card("blocked", ran=False), 1)
        self.assertTrue(epic_clean.held(self.conn, BRANCH))

    def test_the_newest_run_card_holds_unless_done(self):
        for status in ("ready", "blocked", "triage"):
            conn = self.conn
            self.run_at(self.card("done", ran=False), 1)
            self.run_at(self.card(status, ran=False), 9)
            self.assertTrue(epic_clean.held(conn, BRANCH), status)
            conn.execute("DELETE FROM task_runs")
            conn.execute("DELETE FROM tasks")

    def test_a_running_older_card_holds_even_when_the_newest_run_is_done(self):
        self.card("running")
        self.run_at(self.card("done", ran=False), 9)
        self.dirty()
        before = self.status()
        self.run_clean()
        self.assertEqual(self.status(), before)

    def test_a_done_or_never_run_card_does_not_hold_the_clone(self):
        self.card("done")
        self.card("ready", ran=False)
        self.dirty()
        self.run_clean()
        self.assertEqual(self.status(), "")

    def test_a_symlinked_backup_dir_is_refused_and_nothing_is_discarded(self):
        self.dirty()
        elsewhere = self.dir / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.backups)
        before = self.status()
        self.run_clean()
        self.assertEqual(self.status(), before)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_same_second_backup_never_overwrites_an_earlier_one(self):
        self.dirty()
        self.run_clean()
        self.dirty()
        self.run_clean()
        self.assertEqual(len(list(self.backups.glob("*.patch"))), 2)

    def test_a_failed_discard_after_backup_opens_an_incident_naming_it(self):
        self.dirty()
        real = workspace._git

        def fail_reset(*args, **kw):
            if args[:1] == ("reset",):
                raise RuntimeError("boom")
            return real(*args, **kw)
        with mock.patch.object(workspace, "_git", fail_reset):
            self.run_clean()
        patch = next(self.backups.glob("*.patch"))
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM outbox WHERE body LIKE ?", (f"%{patch}%",)).fetchone())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)

    def test_a_conflicting_main_merge_names_the_files_and_does_not_report_cleaned(self):
        (self.origin / "tracked.txt").write_text("main side\n")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "main edit")
        (self.ws / "tracked.txt").write_text("epic side\n")
        git(self.ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "epic edit")
        (self.ws / "fresh.txt").write_text("dirty\n")
        self.run_clean()
        self.assertEqual(len(list(self.backups.glob("*.patch"))), 1)
        fps = [r[0] for r in self.conn.execute("SELECT fingerprint FROM incidents")]
        key = f"epic clean incomplete: {BRANCH}"
        self.assertEqual(fps, [incidents.fingerprint("silent_failure", key)])
        bodies = [r[0] for r in self.conn.execute("SELECT body FROM outbox")]
        self.assertTrue(any("tracked.txt" in b for b in bodies), bodies)
        self.assertFalse(any("self-cleaned" in b for b in bodies), bodies)
        self.assertFalse(any("cleaned, backup" in b for b in bodies), bodies)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM guard_state WHERE key LIKE 'epic_cleaned:%'").fetchone())
        self.assertFalse((workspace.gitdir(self.ws) / "MERGE_HEAD").exists())
        self.assertEqual(self.status(), "")

    def test_a_failed_merge_reason_is_never_empty(self):
        class R:
            returncode, stderr, stdout = 1, b"", b"CONFLICT-ish stdout line\n"
        with mock.patch.object(epic_clean, "_run", return_value=R), \
                mock.patch.object(workspace, "_git"):
            reason = epic_clean._merge_main(self.ws)
        self.assertIn("CONFLICT-ish stdout line", reason)

    def test_a_merge_timeout_opens_an_incident_naming_the_backup(self):
        self.dirty()
        with mock.patch.object(epic_clean, "_merge_main",
                               side_effect=subprocess.TimeoutExpired("git", 1)):
            self.run_clean()
        patch = next(self.backups.glob("*.patch"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM outbox WHERE body LIKE ?", (f"%{patch}%",)).fetchone())

    def test_retention_keeps_the_newest_50_or_30_days_whichever_is_more(self):
        self.backups.mkdir()
        now = 1_000_000_000
        for i in range(60):          # 60 old files: the newest 50 stay, the rest go
            f = self.backups / f"old-{i:02}.patch"
            f.write_text("x")
            os.utime(f, (now - 40 * 86400 - i, now - 40 * 86400 - i))
        for i in range(3):           # recent files are never pruned
            f = self.backups / f"new-{i}.patch"
            f.write_text("x")
            os.utime(f, (now - 86400, now - 86400))
        epic_clean._prune(self.backups, now)
        names = sorted(p.name for p in self.backups.glob("*.patch"))
        self.assertEqual(len(names), 50)
        self.assertEqual(sum(n.startswith("new") for n in names), 3)

    def test_failed_backup_leaves_the_clone_alone_and_logs_an_error(self):
        self.dirty()
        self.backups.write_text("a file where the directory should be")
        before = self.status()
        self.run_clean()
        self.assertEqual(self.status(), before)
        self.assertEqual((self.ws / "fresh.txt").read_text(), "untracked\n")
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM outbox WHERE body LIKE '%backup failed%'").fetchone())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)



if __name__ == "__main__":
    unittest.main()
