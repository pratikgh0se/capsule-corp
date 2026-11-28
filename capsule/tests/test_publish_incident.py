"""`TickRuns._publish_incidents`: a `ready_unreviewed` publish result opens one incident per PR."""
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from capsule import db, tick, tick_runs

T0 = 1_800_000_000
URL = "https://github.com/o/r/pull/7"
HEAD = "abcdef1234567890"


class PublishIncident(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conn = self.board("kanban.db")
        self.task = db.new_task(self.conn, title="card")
        self.folder = self.dir / "lookout" / "publish"
        self.folder.mkdir(parents=True)

    def board(self, name):
        db.create_board(self.dir / name)
        conn = db.connect(self.dir / name)
        self.addCleanup(conn.close)
        return conn

    def result(self, slug="a", cards=None, **doc):
        base = {"pr_url": URL, "pr_number": 7, "head": HEAD}
        (self.folder / f"{slug}.result.json").write_text(json.dumps({**base, **doc}))
        want = [{"task_id": t} for t in (cards or [self.task])]
        (self.folder / f"{slug}.want.json").write_text(json.dumps({"cards": want}))

    def scan(self, now=T0, conn=None, board="b"):
        host = SimpleNamespace(home=self.dir, _unreviewed_key=tick_runs.TickRuns._unreviewed_key)
        conn = conn or self.conn
        tick_runs.TickRuns._publish_incidents(host, conn, {"board": board}, now)
        return conn.execute("SELECT * FROM incidents ORDER BY opened_at").fetchall()

    def test_ready_unreviewed_opens_one_incident_naming_file_head_and_pr(self):
        self.result(ready_unreviewed=True)
        (row,) = self.scan()
        self.assertEqual(row["kind"], "silent_failure")
        self.assertIn("publish_ready_unreviewed:o/r#pr7", row["root_key"])
        for text in (str(self.folder / "a.result.json"), URL, "head:abcdef123456"):
            self.assertIn(text, row["evidence"])

    def test_second_scan_and_next_day_open_nothing_new(self):
        self.result(ready_unreviewed=True)
        self.scan()
        self.assertEqual(len(self.scan(T0 + 5)), 1)
        self.assertEqual(len(self.scan(T0 + 3 * 86400)), 1)

    def test_two_prs_of_one_repo_get_two_incidents(self):
        self.result("a", ready_unreviewed=True)
        self.result("b", ready_unreviewed=True, pr_number=8, pr_url="https://github.com/o/r/pull/8")
        self.assertEqual(len(self.scan()), 2)

    def test_cleared_result_opens_nothing_and_leaves_the_open_incident(self):
        self.result()
        self.assertEqual(self.scan(), [])
        self.result(ready_unreviewed=True)
        self.scan()
        self.result()
        self.assertEqual([r["status"] for r in self.scan(T0 + 9)], ["open"])

    def test_merged_or_closed_pr_opens_nothing(self):
        for state in ("MERGED", "CLOSED"):
            self.result(ready_unreviewed=True, pr={"state": state})
            self.assertEqual(self.scan(), [])

    def test_malformed_results_are_skipped_and_logged_once(self):
        (self.folder / "bad.result.json").write_text("{not json")
        self.result("nonum", ready_unreviewed=True, pr_number="x")
        (self.folder / "list.result.json").write_text("[]")
        self.result("good", ready_unreviewed=True)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(len(self.scan()), 1)
            self.scan(T0 + 1)
        self.assertEqual(err.getvalue().count("bad.result.json skipped"), 1)
        self.assertEqual(err.getvalue().count("nonum.result.json skipped"), 1)

    def test_only_the_board_that_owns_the_pr_opens_it(self):
        other = self.board("other.db")
        self.result(ready_unreviewed=True)
        self.assertEqual(self.scan(conn=other, board="other"), [])
        self.assertEqual(len(self.scan()), 1)

    def test_closed_incident_is_not_repeated_at_the_same_head_but_is_at_a_new_head(self):
        self.result(ready_unreviewed=True)
        (row,) = self.scan()
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?",
                          (T0 + 1, row["id"]))
        self.assertEqual(len(self.scan(T0 + 2)), 1)
        self.result(ready_unreviewed=True, head="1234567890abcdef")
        self.assertEqual(len(self.scan(T0 + 3)), 2)

    def test_a_reader_failure_is_counted_and_tick_pass_still_runs(self):
        def boom(*args):
            raise RuntimeError("boom")
        host = SimpleNamespace(home=self.dir, _publish_incidents=boom, _incident_gate=lambda *a: 0,
                               _spool_root=lambda cfg: self.dir)
        guarded = tick_runs.TickRuns._publish_incidents_guarded
        host._publish_incidents_guarded = lambda *a: guarded(host, *a)
        counts = {"tick_errors": 0}
        with redirect_stderr(io.StringIO()), mock.patch.object(tick, "tick_pass") as passed:
            tick.Tick._incidents(host, self.conn, {"board": "b", "routing": {}}, T0, counts)
        self.assertEqual(counts["tick_errors"], 1)
        passed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
