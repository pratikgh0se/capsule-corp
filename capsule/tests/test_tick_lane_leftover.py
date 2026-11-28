"""Row 624: a card launched on a reused lane clone never commits leftover dirt as its own work;
`tick_runs._guard_launch` sets it aside (or blocks the card when it cannot)."""
import json
import os

from capsule import db, paths, workspace
from capsule.tests.test_tick import BRANCH, IMPL_DONE, TickCase


class LaneLeftover(TickCase):
    def _reused_clone(self):
        work = paths.board_dir("test", self.home) / "work"
        return workspace.clone(self.cfg["repo_url"], BRANCH, work)

    def test_untracked_leftover_in_a_reused_clone_is_set_aside_before_the_card_launches(self):
        ws = self._reused_clone()
        (ws / "leftover.txt").write_text("not this card's\n")
        self.script["implementer"] = [[IMPL_DONE]]
        self.tick.once()
        impl = self.runs("implementer")
        self.assertEqual(len(impl), 1)
        self.assertEqual(json.loads(impl[0]["metadata"])["start_sha"], workspace.head(ws))
        self.assertFalse((ws / "leftover.txt").exists())
        backups = paths.board_dir("test", self.home) / "epic-backups"
        self.assertEqual(len(list(backups.glob("*.patch"))), 1)

    def _failing_backups(self):
        ws = self._reused_clone()
        (ws / "leftover.txt").write_text("not this card's\n")
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, paths.board_dir("test", self.home) / "epic-backups")
        self.script["implementer"] = [[IMPL_DONE]]
        return ws

    def test_a_failed_backup_leaves_every_ready_card_unclaimed_with_one_notice(self):
        ws = self._failing_backups()
        other = db.new_task(self.conn, title="second", assignee="dev-frontend", branch=BRANCH,
                            status="ready")
        self.tick.once()
        notices = self.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE body LIKE '%backup failed%'").fetchone()[0]
        self.tick.once()
        self.assertEqual(self.runs("implementer"), [])
        states = [r[0] for r in self.conn.execute(
            "SELECT status FROM tasks WHERE id IN (?,?)", (self.tid, other))]
        self.assertEqual(states, ["ready", "ready"])
        self.assertTrue((ws / "leftover.txt").exists())
        self.assertEqual(notices, 1)
