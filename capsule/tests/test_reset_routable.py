"""t_6eb16658: a human `reset` never leaves a card the dispatcher skips (no epic/assignee)."""
from capsule import db, dispatch, interventions
from capsule.tests.test_tick import BRANCH, TickCase


class ResetRoutable(TickCase):
    def _triage_card(self):
        tid = db.new_task(self.conn, title="orphan", assignee="dev-frontend", branch=BRANCH, now=5)
        self.conn.execute("UPDATE tasks SET status='triage', assignee=NULL, branch_name=NULL"
                          " WHERE id=?", (tid,))
        return tid

    def _reset(self, tid):
        doc = {"kind": "reset", "text": "go"}
        interventions.apply(self.conn, self.cfg, tid, doc, "r.json", "r.json", 10)

    def test_reset_fills_the_single_epic_and_dev_backend(self):
        tid = self._triage_card()
        self._reset(tid)
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["status"], row["branch_name"], row["assignee"]),
                         ("ready", BRANCH, "dev-backend"))
        self.assertIsNotNone(dispatch.resolve_assignee(self.cfg, row["assignee"]))

    def test_reset_refuses_loudly_when_the_epic_cannot_be_chosen(self):
        tid = self._triage_card()
        self.cfg["epics"] = [BRANCH, "epic/other"]
        self._reset(tid)
        row = self.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["status"], "triage")
        refused = self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='reset_refused'",
            (tid,)).fetchall()
        self.assertEqual(len(refused), 1)
        self.assertIn("epic", refused[0][0])

    def test_reset_ignores_a_running_card(self):
        tid = self._triage_card()
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        self._reset(tid)
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["branch_name"], row["assignee"]), (None, None))

    def test_epic_then_reset_readies_a_refused_triage_card(self):
        tid = self._triage_card()
        self.cfg["epics"] = [BRANCH, "epic/other"]
        self._reset(tid)
        interventions.apply(self.conn, self.cfg, tid, {"kind": "epic", "text": BRANCH},
                            "e.json", "e.json", 11)
        self._reset(tid)
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["status"], row["branch_name"]), ("ready", BRANCH))
        self.assertIsNotNone(dispatch.resolve_assignee(self.cfg, row["assignee"]))
