"""S5 (redesign.deps_ready), second oracle (Z2): the dispatch events in `task_events` after real
`tick.once()` passes, next to `test_dispatch.DepsReady`'s own read of `ready()`."""
from capsule import db
from capsule.tests.test_tick import BRANCH, IMPL_DONE, TickCase


class DepsReadyTick(TickCase):
    def setUp(self):
        super().setUp()
        self.parent = db.new_task(self.conn, title="parent", assignee="dev-frontend", branch=BRANCH,
                                  status="blocked", now=1)
        # created first, so `ready` offers it before the independent card `self.tid`
        self.child = db.new_task(self.conn, title="child", assignee="dev-frontend", branch=BRANCH,
                                 parents=[self.parent], now=2)
        self.script["implementer"] = [[IMPL_DONE]]

    def switch(self, on):
        self.cfg["redesign"] = {"deps_ready": on}
        self.write_cfg()
        self.tick = self.make_tick()

    def kinds(self, task_id):
        return [r["kind"] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task_id,))]

    def status(self, task_id):
        return self.conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]

    def test_switch_on_the_held_child_is_never_claimed_or_bounced_until_the_parent_lands(self):
        self.switch(True)
        for _ in range(6):
            self.tick.once()
            if self.status(self.tid) == "done":
                break
        self.assertIn("claimed", self.kinds(self.tid))
        self.assertEqual(self.status(self.tid), "done")
        self.assertEqual(self.status(self.child), "ready")
        for kind in ("claimed", "claim_rejected"):
            self.assertNotIn(kind, self.kinds(self.child))
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.parent,))
        self.conn.commit()
        self.tick.once()
        self.assertIn("claimed", self.kinds(self.child))

    def test_switch_off_the_child_is_claimed_into_a_bounce_and_goes_to_todo(self):
        self.switch(False)
        self.tick.once()
        self.assertIn("claimed", self.kinds(self.tid))
        self.assertIn("claim_rejected", self.kinds(self.child))
        self.assertNotIn("claimed", self.kinds(self.child))
        self.assertEqual(self.status(self.child), "todo")
