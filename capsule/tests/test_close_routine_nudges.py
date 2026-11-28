# mutation-check: targets-predate-range b957739c9 -- renames only, no behaviour pinned ::
#   CloseRoutineNudges.test_only_cos_routine_incidents_close
"""capsule/close_routine_nudges.py: only open routine cos nudge incidents close. Temp board only."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import close_routine_nudges, db, incidents, paths

T0 = 1_790_000_000


class CloseRoutineNudges(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        path = paths.board_db("b", self.dir)
        path.parent.mkdir(parents=True)
        db.create_board(path)
        self.conn = db.connect(path)
        self.addCleanup(self.conn.close)
        self.tid = db.new_task(self.conn, title="card", assignee="dev-frontend", branch="epic/x")

    def nudge(self, kind, author, n):
        payload = {"kind": kind, "file": f"{n}.json", "author": author}
        db.event(self.conn, self.tid, "intervention", payload, now=T0)
        eid = self.conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
        inc, _ = incidents.open_incident(self.conn, board="b", kind="human_intervention",
                                         key=f"{kind}: {n}", evidence=[f"event:{eid}"],
                                         task_id=self.tid, now=T0)
        return inc

    def run_script(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            argv = ["--board", "b", "--capsule-home", str(self.dir)]
            self.assertEqual(close_routine_nudges.main(argv), 0)
        return out.getvalue()

    def test_only_cos_routine_incidents_close(self):
        routine = [self.nudge(k, "cos", n) for n, k in enumerate(incidents.ROUTINE_COS_KINDS)]
        kept = [self.nudge("reset", "owner", 20), self.nudge("override", "cos", 21),
                self.nudge("accept", None, 22)]
        other, _ = incidents.open_incident(self.conn, board="b", kind="crash", key="boom",
                                           evidence=["event:1"], now=T0)
        kept.append(other)
        before = {i: incidents.get(self.conn, i) for i in kept}
        self.assertIn(f"closed {len(routine)}, left 3", self.run_script())
        for i in routine:
            self.assertEqual(incidents.get(self.conn, i)["status"], "closed")
        for i in kept:
            self.assertEqual(incidents.get(self.conn, i), before[i])
        reasons = [json.loads(r[0]).get("reason") for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='incident_closed'")]
        self.assertEqual(reasons, [incidents.ROUTINE_REASON] * len(routine))

    def test_close_without_reason_is_still_gated(self):
        inc = self.nudge("assign", "cos", 1)
        with self.assertRaises(incidents.CloseRefused):
            incidents.close(self.conn, inc, vault="")
        self.assertEqual(incidents.get(self.conn, inc)["status"], "open")

    def test_second_run_closes_nothing(self):
        self.nudge("assign", "cos", 1)
        self.run_script()
        self.assertIn("closed 0, left 0", self.run_script())


if __name__ == "__main__":
    unittest.main()
