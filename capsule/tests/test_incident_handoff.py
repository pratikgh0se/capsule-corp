"""One CoS hand-off (an answer plus the unblock that points back to it) opens one incident
(t_e5c70672, inc_5b420323): incidents._fold_target through incidents.from_events."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import db, human, incidents

T0 = 1_793_525_978


class OneHandOff(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        db.create_board(self.dir / "kanban.db")
        self.conn = db.connect(self.dir / "kanban.db")
        self.addCleanup(self.conn.close)
        self.tid = db.new_task(self.conn, title="card", assignee="dev-frontend", branch="epic/x")
        incidents.from_events(self.conn, board="b", now=T0)      # sets the watermark
        self.n = 0

    def hand(self, kind, at=T0, author="cos", task=None, **more):
        self.n += 1
        db.event(self.conn, task or self.tid, "intervention",
                 {"kind": kind, "at": at, "author": author, "file": f"{self.n}.json",
                  "src": f"{self.n}.json", **more}, now=T0)

    def run_pass(self):
        return incidents.from_events(self.conn, board="b", human_dir=self.dir / "human", now=T0)

    def opened(self):
        return [r for r in incidents.listing(self.conn) if r["kind"] == "human_intervention"]

    def pair(self, **unblock):
        self.hand("answer")
        self.hand("unblock", **unblock)

    def test_an_answer_and_its_unblock_from_one_hand_off_open_one_incident(self):
        self.pair()
        self.run_pass()
        (only,) = self.opened()
        evidence = json.loads(only["evidence"])
        events = [r["id"] for r in self.conn.execute("SELECT id FROM task_events")
                  if f"event:{r['id']}" in evidence]
        self.assertEqual(len(events), 2)
        for name in ("1.json", "2.json"):
            self.assertIn(str(self.dir / "human" / "done" / name), evidence)

    def test_a_legacy_author_answer_and_an_owner_unblock_fold_into_one_incident(self):
        self.hand("answer", author=human.LEGACY_OWNER)
        self.hand("unblock", author="owner")
        self.run_pass()
        self.assertEqual(len(self.opened()), 1)

    def test_the_blind_gate_accept_reports_owner_for_a_legacy_author(self):
        from capsule.blind import gate
        self.hand("accept", author=human.LEGACY_OWNER, text="waive it", outcome="applied")
        self.assertEqual(gate._accepted(self.conn, self.tid, 0)[3], "owner")

    def test_the_same_result_when_the_two_events_arrive_in_two_passes(self):
        self.hand("answer")
        self.run_pass()
        self.hand("unblock")
        self.run_pass()
        (only,) = self.opened()
        self.assertEqual(len([e for e in json.loads(only["evidence"]) if e.endswith(".json")]), 2)

    def test_the_answer_alone_still_opens_its_incident(self):
        self.hand("answer")
        self.run_pass()
        self.assertEqual(len(self.opened()), 1)

    def test_a_refused_unblock_folds_the_same_way(self):
        self.pair()
        self.conn.execute("UPDATE task_events SET payload=json_set(payload, '$.outcome',"
                          " 'refused:not_blocked') WHERE id=(SELECT MAX(id) FROM task_events)")
        self.run_pass()
        self.assertEqual(len(self.opened()), 1)

    def test_a_different_author_opens_a_second_incident(self):
        self.pair(author="owner")
        self.run_pass()
        self.assertEqual(len(self.opened()), 2)

    def test_a_gap_above_the_window_opens_a_second_incident(self):
        self.pair(at=T0 + 601)
        self.run_pass()
        self.assertEqual(len(self.opened()), 2)

    def test_a_gap_at_the_window_still_folds(self):
        self.pair(at=T0 + 600)
        self.run_pass()
        self.assertEqual(len(self.opened()), 1)

    def test_a_closed_answer_incident_opens_a_second_incident(self):
        self.hand("answer")
        self.run_pass()
        self.conn.execute("UPDATE incidents SET status='closed'")
        self.hand("unblock")
        self.run_pass()
        self.assertEqual(len(self.opened()), 2)

    def test_an_earlier_intervention_that_is_not_an_answer_opens_a_second_incident(self):
        self.hand("reset", author="owner")
        self.hand("unblock", author="owner")
        self.run_pass()
        self.assertEqual(len(self.opened()), 2)

    def test_an_intervening_hand_action_opens_a_second_incident(self):
        self.hand("answer")
        self.hand("reset")
        self.hand("unblock")
        self.run_pass()
        self.assertEqual(len(self.opened()), 2)     # the cos reset is routine: answer + unblock

    def test_an_unblock_with_no_earlier_answer_opens_an_incident(self):
        self.hand("unblock")
        self.run_pass()
        self.assertEqual(len(self.opened()), 1)

    def test_a_later_hand_off_folds_into_its_own_answer_incident(self):
        self.pair()
        self.hand("answer", at=T0 + 5000)
        self.hand("unblock", at=T0 + 5001)
        self.run_pass()
        self.assertEqual(len(self.opened()), 2)
        answer2, unblock2 = [r["id"] for r in self.conn.execute(
            "SELECT id FROM task_events WHERE kind='intervention' ORDER BY id DESC LIMIT 2")][::-1]
        (holder,) = [json.loads(r["evidence"]) for r in self.opened()
                     if f"event:{unblock2}" in json.loads(r["evidence"])]
        self.assertIn(f"event:{answer2}", holder)      # the second answer's own incident

    def test_a_superseded_answer_incident_opens_a_second_incident(self):
        self.hand("answer")
        self.run_pass()
        self.conn.execute("UPDATE incidents SET status='superseded'")
        self.hand("unblock")
        self.run_pass()
        self.assertEqual(len(self.opened()), 2)

    # an answer incident opened as kind `repeat` never takes the fold (overcounts, safe direction)

    def test_two_files_with_no_author_do_not_fold(self):
        self.hand("answer", author=None)
        self.hand("unblock", author=None)
        self.run_pass()
        self.assertEqual(len(self.opened()), 2)

    def test_a_block_between_the_answer_and_the_unblock_opens_a_second_incident(self):
        self.hand("answer", at=T0)
        self.run_pass()
        db.event(self.conn, self.tid, "blocked", {"kind": "needs_input", "reason": "new?"}, now=T0)
        self.hand("unblock", at=T0 + 400)
        self.run_pass()
        self.assertEqual(len(self.opened()), 2)

    def test_two_cards_in_one_batch_get_one_incident_each(self):
        other = db.new_task(self.conn, title="other", assignee="dev-frontend", branch="epic/x")
        self.hand("answer")
        self.hand("answer", task=other)
        self.hand("unblock")
        self.hand("unblock", task=other)
        self.run_pass()
        self.assertEqual(sorted(r["task_id"] for r in self.opened()), sorted([self.tid, other]))


if __name__ == "__main__":
    unittest.main()
