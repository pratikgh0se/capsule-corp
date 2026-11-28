import json
import tempfile
import unittest
from pathlib import Path

from capsule import (blocks, db, human, incidents, interventions, invariants, lookout, outbox,
                     slack_msgs)


class EpicMergeWait(unittest.TestCase):
    """inc_b862eccd: a card waits on a declared epic branch; `reset` is the hand release.
    `test_blocks` imports this class."""
    DECLARED = ["epic/a", "epic/b", "epic/x", "epic/y"]

    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "kanban.db"
        db.create_board(path)
        self.conn = db.connect(path)
        self.addCleanup(self.conn.close)
        self.cfg = {"board": "t", "epics": self.DECLARED}

    def card(self, status="ready", branch="epic/b", block_kind=None):
        tid = db.new_task(self.conn, title="t", status=status, branch=branch)
        if block_kind:
            self.conn.execute("UPDATE tasks SET block_kind=? WHERE id=?", (block_kind, tid))
        return tid

    def wait(self, tid, branch="epic/a"):
        return blocks.wait(self.conn, tid, branch, declared=self.DECLARED)

    def row(self, tid):
        return self.conn.execute("SELECT status, block_kind, waits_on_branch, block_recurrences"
                                 " FROM tasks WHERE id=?", (tid,)).fetchone()

    def kinds(self, tid, kind):
        return [json.loads(r[0] or "{}") for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind))]

    def test_promote_ready_leaves_a_waiting_card(self):
        tid = self.card()
        self.wait(tid)
        self.assertEqual(blocks.promote_ready(self.conn), [])
        self.assertEqual(tuple(self.row(tid))[:3], ("todo", "dependency", "epic/a"))

    # -- the status matrix: one test per accepted and refused status
    def _accepts(self, status, block_kind=None):
        tid = self.card(status, block_kind=block_kind)
        self.assertEqual(self.wait(tid), "todo")
        self.assertEqual(tuple(self.row(tid))[:3], ("todo", "dependency", "epic/a"))
        self.assertEqual(self.kinds(tid, "wait_refused"), [])

    def _refuses(self, status):
        tid = self.card(status)
        self.assertIsNone(self.wait(tid))
        refused = self.kinds(tid, "wait_refused")
        self.assertEqual(refused, [{"branch": "epic/a", "reason": f"{status}: cannot wait"}])
        self.assertEqual(tuple(self.row(tid))[:3], (status, None, None))

    def _refuses_block(self, kind):
        tid = self.card("blocked", block_kind=kind)
        self.assertIsNone(self.wait(tid))
        reason = self.kinds(tid, "wait_refused")[0]["reason"]
        self.assertEqual(reason, f"{kind}: reset/accept first")
        self.assertEqual(tuple(self.row(tid))[:3], ("blocked", kind, None))

    def test_matrix_accepts_ready(self):
        self._accepts("ready")

    def test_matrix_accepts_todo(self):
        self._accepts("todo")

    def test_matrix_accepts_blocked(self):
        self._accepts("blocked", "needs_input")

    def test_matrix_refuses_running(self):
        self._refuses("running")

    def test_matrix_refuses_review(self):
        self._refuses("review")

    def test_matrix_refuses_scheduled(self):
        self._refuses("scheduled")

    def test_matrix_refuses_triage(self):
        self._refuses("triage")

    def test_matrix_refuses_done(self):
        self._refuses("done")

    def test_matrix_refuses_parked_block(self):
        self._refuses_block("parked")

    def test_matrix_refuses_spec_stale_block(self):
        self._refuses_block("spec_stale")

    def test_blind_held_card_refuses_wait_and_plain_needs_input_accepts(self):
        tid = self.card("blocked", block_kind="needs_input")
        self.conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at)"
                          " VALUES (?, 'blocked', ?, 1)",
                          (tid, json.dumps({"cause": "blind_gate:abc:reason"})))
        self.assertIsNone(self.wait(tid))
        self.assertEqual(tuple(self.row(tid))[:2], ("blocked", "needs_input"))
        self.assertIn("blind", self.kinds(tid, "wait_refused")[0]["reason"])
        self.assertEqual(self.wait(self.card("blocked", block_kind="needs_input")), "todo")

    def test_wait_from_blocked_leaves_no_lost_card_state(self):
        tid = self.card("running")
        self.assertEqual(invariants._lost_card_state(self.conn), [])   # sets the baseline
        blocks.block(self.conn, tid, kind="needs_input", reason="needs stats.py")
        self.wait(tid)
        self.assertEqual(len(self.kinds(tid, "unblocked")), 1)
        self.assertEqual(invariants._lost_card_state(self.conn), [])
        blocks.reset_failures(self.conn, tid, by="human")
        self.assertEqual(invariants._lost_card_state(self.conn), [])

    def test_wait_from_blocked_leaves_no_open_needs_input_block_for_lookout(self):
        tid = self.card("running")
        blocks.block(self.conn, tid, kind="needs_input", reason="needs stats.py")
        self.wait(tid)
        home = Path(tempfile.mkdtemp())
        self.cfg["caps"] = {"tick_seconds": 30}
        col = lookout.Collector(home, "t", host="h", pid=1)
        col.collect(self.conn, self.cfg, 2000, dict.fromkeys(("lookout_errors",), 0), pass_ms=5)
        lines = [json.loads(x) for x in lookout.spool_path(home).read_text().splitlines()]
        rows = [x["row"] for x in lines if x.get("table") == "cap_blocks"
                and x["row"]["task_id"] == tid]
        opened = [r for r in rows if r["closed_at"] is None]
        self.assertEqual([r["kind"] for r in opened], ["dependency"])
        needs = [r for r in rows if r["kind"] == "needs_input"]
        self.assertEqual([r["closed_by"] for r in needs], ["unblocked"])

    def test_wait_refuses_undeclared_branch(self):
        tid = self.card()
        self.assertIsNone(self.wait(tid, "epic/nope"))
        self.assertEqual(self.kinds(tid, "wait_refused"),
                         [{"branch": "epic/nope", "reason": "epic/nope: not declared"}])
        self.assertEqual(tuple(self.row(tid))[:3], ("ready", None, None))

    def test_wait_refuses_a_missing_card(self):
        self.assertIsNone(self.wait("t_missing"))
        events = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='wait_refused'")]
        self.assertEqual([e["reason"] for e in events], ["no such task"])

    def test_wait_refuses_the_cards_own_branch(self):
        tid = self.card(branch="epic/a")
        self.assertIsNone(self.wait(tid))
        self.assertEqual(self.kinds(tid, "wait_refused")[0]["reason"], "own branch")

    def test_a_refusal_notifies_and_changes_nothing(self):
        tid = self.card("blocked", block_kind="needs_input")
        self.conn.execute("UPDATE tasks SET block_recurrences=1 WHERE id=?", (tid,))
        self.assertIsNone(self.wait(tid, "epic/nope"))
        self.assertEqual(tuple(self.row(tid)), ("blocked", "needs_input", None, 1))
        sent = self.conn.execute("SELECT COUNT(*) FROM outbox WHERE body LIKE '%wait refused%'")
        self.assertEqual(sent.fetchone()[0], 1)

    def test_a_wait_does_not_bump_recurrence(self):
        tid = self.card("blocked", block_kind="needs_input")
        self.wait(tid)
        self.assertEqual(self.row(tid)["block_recurrences"], 0)

    def test_needs_input_to_wait_closes_the_needs_message(self):
        outbox.set_slack(self.conn, True)
        tid = self.card("running")
        blocks.block(self.conn, tid, kind="needs_input", reason="needs stats.py")
        self.assertEqual(self.row(tid)["status"], "blocked")
        self.assertEqual(self.wait(tid), "todo")
        lines = self.conn.execute("SELECT o.body FROM outbox o JOIN outbox_key k"
                                  " ON k.outbox_id=o.id WHERE k.dedup_key=? ORDER BY o.id",
                                  (slack_msgs.needs_key(tid),)).fetchall()
        self.assertTrue(lines[-1][0].startswith(slack_msgs.HANDLED))

    def test_park_clears_the_wait(self):
        tid = self.card()
        self.wait(tid)
        blocks.park(self.conn, tid, "vendor")
        self.assertEqual(tuple(self.row(tid))[:3], ("blocked", "parked", None))

    def test_reset_clears_the_wait_and_the_card_is_ready_again(self):
        tid = self.card()
        self.wait(tid)
        blocks.reset_failures(self.conn, tid, by="human")
        self.assertEqual(tuple(self.row(tid))[:3], ("todo", None, None))
        self.assertEqual(blocks.promote_ready(self.conn), [tid])
        self.assertEqual(self.row(tid)["status"], "ready")

    def test_wait_then_reset_leaves_no_open_lookout_block(self):
        tid = self.card()
        self.wait(tid)
        blocks.reset_failures(self.conn, tid, by="human")
        home = Path(tempfile.mkdtemp())
        self.cfg["caps"] = {"tick_seconds": 30}
        col = lookout.Collector(home, "t", host="h", pid=1)
        col.collect(self.conn, self.cfg, 2000, dict.fromkeys(("lookout_errors",), 0), pass_ms=5)
        lines = [json.loads(x) for x in lookout.spool_path(home).read_text().splitlines()]
        opened = [x["row"] for x in lines if x.get("table") == "cap_blocks"
                  and x["row"]["task_id"] == tid and x["row"]["closed_at"] is None]
        self.assertEqual(opened, [])

    # -- the CoS verb: the outcome stamp on the intervention event
    def _stamped(self, tid):
        row = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND"
                                " kind='intervention' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        return json.loads(row[0])["outcome"]

    def _intervene(self, tid, branch):
        doc = {"kind": "wait", "text": branch, "at": "t"}
        interventions.apply(self.conn, self.cfg, tid, doc, "w.json", "w.json", 1000)
        return self._stamped(tid)

    def test_the_wait_verb_stamps_applied(self):
        tid = self.card()
        self.assertEqual(self._intervene(tid, "epic/a"), "applied")
        self.assertEqual(tuple(self.row(tid))[:3], ("todo", "dependency", "epic/a"))

    def test_the_wait_verb_stamps_the_refusal_cause(self):
        tid = self.card()
        self.assertEqual(self._intervene(tid, "epic/nope"), "refused:epic/nope: not declared")

    def test_a_cos_wait_intervention_opens_no_incident(self):
        tid = self.card()
        incidents.from_events(self.conn, board="t", now=1000)   # sets the watermark
        db.event(self.conn, tid, "intervention", {"kind": "wait", "file": "w.json",
                                                  "src": "w.json", "author": "cos"})
        self.assertEqual(incidents.from_events(self.conn, board="t", now=1000), [])

    def test_human_write_refuses_a_blank_wait_branch(self):
        home = Path(tempfile.mkdtemp())
        with self.assertRaises(ValueError):
            human.write(home, "t", self.card(), "wait", "  ")
        self.assertEqual([p for p in home.rglob("*") if p.is_file()], [])
