"""L399: `capsule human <board> <task> archive '<why>'` takes a card that will never resume out of
its lane. Oracle 1: the card's status and `epic_clean.holders` (a parked card that ran holds the
lane until then). Oracle 2: the intervention event's author, the refusal events, and that nothing
was deleted."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, epic_clean, human, lease, paths
from capsule.__main__ import main
from capsule.tests.test_human_author import session

BOARD = "bx"
BRANCH = "epic/x"
NOW = 1_800_000_000


class ArchiveVerb(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        paths.board_dir(BOARD, self.home).mkdir(parents=True)
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        paths.config_path(self.home, BOARD).write_text(json.dumps(dict(cfg, board=BOARD)))
        db.create_board(paths.board_db(BOARD, self.home))
        self.conn = db.connect(paths.board_db(BOARD, self.home))
        self.addCleanup(self.conn.close)
        self.tid = db.new_task(self.conn, title="parked", assignee="dev-backend", branch=BRANCH,
                               status="ready", now=NOW - 100)
        lease.start_run(self.conn, self.tid, role="implementer", claim_lock=None, expires=0,
                        run_id="r-1", now=NOW - 90)
        self.conn.commit()

    def status(self, tid=None):
        return self.conn.execute("SELECT status FROM tasks WHERE id=?",
                                 (tid or self.tid,)).fetchone()[0]

    def set_status(self, status):
        self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, self.tid))
        self.conn.commit()

    def apply(self, tid, text="never resumed"):
        human.write(self.home, BOARD, tid, "archive", text, author="cos", now=NOW)
        with mock.patch.dict(os.environ, {"AGENT_OPS_NO_SEND": "1"}), \
                contextlib.redirect_stdout(io.StringIO()):
            return main(["human-apply", "--board", BOARD, "--capsule-home", str(self.home)])

    def events(self, kind):
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (self.tid, kind))]

    def test_archive_releases_a_parked_card_from_its_lane(self):
        self.set_status("blocked")
        self.conn.execute("UPDATE tasks SET block_kind='parked' WHERE id=?", (self.tid,))
        self.conn.commit()
        self.assertEqual(epic_clean.holders(self.conn, BRANCH), {self.tid})
        self.assertEqual(self.apply(self.tid), 0)
        self.assertEqual(self.status(), "archived")
        self.assertEqual(epic_clean.holders(self.conn, BRANCH), set())
        self.assertFalse(epic_clean.held(self.conn, BRANCH))

    def test_the_intervention_and_the_archive_are_recorded_with_the_author(self):
        self.set_status("blocked")
        self.apply(self.tid, "superseded by a new plan")
        self.assertEqual(self.events("intervention")[0]["author"], "cos")
        done = self.events("archived")[0]
        self.assertEqual((done["by"], done["reason"], done["was"]),
                         ("human", "superseded by a new plan", "blocked"))

    def test_a_live_or_review_card_is_refused_and_keeps_its_state(self):
        for status in ("running", "review"):
            self.set_status(status)
            self.apply(self.tid)
            self.assertEqual(self.status(), status)
        self.set_status("blocked")
        self.conn.execute("UPDATE tasks SET claim_lock='w1' WHERE id=?", (self.tid,))
        self.conn.commit()
        self.apply(self.tid)
        self.assertEqual(self.status(), "blocked")
        self.assertEqual(len(self.events("archive_refused")), 3)
        self.assertEqual(self.events("archived"), [])

    def test_a_done_card_is_refused(self):
        self.set_status("done")
        self.apply(self.tid)
        self.assertEqual(self.status(), "done")
        self.assertEqual(self.events("archive_refused")[0]["reason"], "done: cannot archive")

    def test_cli_names_an_unknown_task_and_writes_nothing(self):
        err = io.StringIO()
        with session(), contextlib.redirect_stderr(err):
            rc = main(["human", BOARD, "t_nope", "archive", "gone", "--author", "cos",
                       "--capsule-home", str(self.home)])
        self.assertEqual(rc, 1)
        self.assertIn("t_nope: no such task", err.getvalue())
        self.assertEqual(list(paths.board_dir(BOARD, self.home).glob("human/*.json")), [])

    def test_cli_refuses_a_running_card_before_writing(self):
        self.set_status("running")
        err = io.StringIO()
        with session(), contextlib.redirect_stderr(err):
            rc = main(["human", BOARD, self.tid, "archive", "gone", "--author", "cos",
                       "--capsule-home", str(self.home)])
        self.assertEqual(rc, 1)
        self.assertIn("running: cannot archive", err.getvalue())

    def test_a_blank_reason_never_reaches_the_spool(self):
        with self.assertRaises(ValueError):
            human.write(self.home, BOARD, self.tid, "archive", "  ")

    def test_unfinished_children_refuse_the_archive_until_they_finish(self):
        kid = db.new_task(self.conn, title="kid", branch=BRANCH, status="todo",
                          parents=(self.tid,), now=NOW)
        self.set_status("blocked")
        self.apply(self.tid)
        self.assertEqual(self.status(), "blocked")
        self.assertEqual(self.events("archive_refused")[0]["reason"],
                         f"has unfinished children: {kid}")
        self.assertEqual(self.status(kid), "todo")
        self.assertIn(f"has unfinished children: {kid}", self.cli_refusal())
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (kid,))
        self.conn.commit()
        self.apply(self.tid)
        self.assertEqual(self.status(), "archived")

    def test_an_open_incidents_fix_card_is_refused(self):
        self.set_status("blocked")
        self.conn.execute(
            "INSERT INTO incidents (id, board, kind, opened_at, fingerprint, status, fix_card)"
            " VALUES ('inc_1', ?, 'crash', 1, 'fp', 'fix_pending', ?)", (BOARD, self.tid))
        self.conn.commit()
        self.apply(self.tid)
        self.assertEqual(self.status(), "blocked")
        self.assertEqual(self.events("archive_refused")[0]["reason"],
                         "fix card of open incident inc_1")
        self.assertIn("fix card of open incident inc_1", self.cli_refusal())
        self.conn.execute("UPDATE incidents SET status='closed' WHERE id='inc_1'")
        self.conn.commit()
        self.apply(self.tid)
        self.assertEqual(self.status(), "archived")

    def cli_refusal(self):
        err = io.StringIO()
        with session(), contextlib.redirect_stderr(err):
            rc = main(["human", BOARD, self.tid, "archive", "gone", "--author", "cos",
                       "--capsule-home", str(self.home)])
        self.assertEqual(rc, 1)
        return err.getvalue()


if __name__ == "__main__":
    unittest.main()
