import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from capsule import __main__ as cli
from capsule import config, db, dispatch, invariants, lookout, replay, report, unblocked
from capsule import event_kinds, workspace
from capsule.tests.test_dispatch import CFG, EPIC, board, ids


class Briefs(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.on = dict(CFG, design_briefs=True)

    def card(self, conn, flags=("data",)):
        return db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC,
                           risk_flags=flags)

    def ready(self, conn, cfg):
        return ids(dispatch.ready(conn, cfg, {"strong"}, 1000, self.home))

    def events(self, conn, tid):
        return conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'",
                            (tid,)).fetchall()

    def test_off_mode_matches_missing_key_and_holds_nothing(self):
        conn = board(self)
        tid = self.card(conn)
        want = self.ready(conn, CFG)
        self.assertEqual(want, [tid])
        self.assertEqual(self.ready(conn, dict(CFG, design_briefs=False)), want)
        self.assertEqual(self.events(conn, tid), [])

    def test_risky_card_held_with_one_event_then_returns_when_file_exists(self):
        conn = board(self)
        tid = self.card(conn)
        self.assertEqual(self.ready(conn, self.on), [])
        self.assertEqual(self.ready(conn, self.on), [])
        got = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND"
                           " kind='design_brief_missing'", (tid,)).fetchall()
        self.assertEqual(len(got), 1)
        payload = json.loads(got[0][0])
        self.assertTrue(payload["path"].endswith(f"docs/design/{tid}.md"))
        self.assertEqual(self.events(conn, tid), [])
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row[0], "ready")
        clone = self.home / "boards" / CFG["board"] / "work" / workspace._slug(EPIC)
        path = clone / "docs" / "design" / f"{tid}.md"
        path.parent.mkdir(parents=True)
        path.write_text("# brief\n")
        self.assertEqual(self.ready(conn, self.on), [tid])
        path.unlink()
        self.assertEqual(self.ready(conn, self.on), [])      # gone again: no second event
        self.assertEqual(self.missing_events(conn, tid), 1)

    def missing_events(self, conn, tid):
        return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND"
                            " kind='design_brief_missing'", (tid,)).fetchone()[0]

    def test_empty_flags_in_every_form_are_never_held(self):
        conn = board(self)
        tid = self.card(conn, flags=())
        self.assertEqual(self.ready(conn, self.on), [tid])
        for raw in (None, "", "[]"):
            conn.execute("UPDATE tasks SET risk_flags=? WHERE id=?", (raw, tid))
            self.assertEqual(self.ready(conn, self.on), [tid], raw)
        self.assertEqual(self.missing_events(conn, tid), 0)
        self.assertEqual(self.events(conn, tid), [])

    def test_held_card_is_invisible_to_invariants_lookout_report_and_replay(self):
        conn = board(self)
        tid = self.card(conn)
        invariants._lost_card_state(conn)                    # the baseline pass
        self.assertEqual(self.ready(conn, self.on), [])
        self.assertEqual(invariants._lost_card_state(conn), [])
        kind = "design_brief_missing"
        for group in (lookout.BLOCK_OPEN, report.FAILURE_EVENTS, invariants.IMPLIES,
                      invariants.STATE_EVENTS):
            self.assertNotIn(kind, group)
        self.assertEqual(report._card_failures(conn, tid, 0), ([], []))
        self.assertIn(kind, replay._NO_MOVE)
        self.assertEqual(event_kinds.ALLOWED[kind], frozenset({"path"}))

    def test_unblocked_names_design_brief_then_clears(self):
        conn = board(self)
        tid = self.card(conn)
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        caps = {"board": CFG["board"], "design_briefs": True}
        self.assertEqual(unblocked.dispatcher_hold_reason(conn, row, set(), caps, self.home),
                         "design-brief")
        self.assertIsNone(unblocked.dispatcher_hold_reason(conn, row, set(), {}, self.home))
        clone = self.home / "boards" / CFG["board"] / "work" / workspace._slug(EPIC)
        (clone / "docs" / "design").mkdir(parents=True)
        (clone / "docs" / "design" / f"{tid}.md").write_text("# brief\n")
        self.assertIsNone(unblocked.dispatcher_hold_reason(conn, row, set(), caps, self.home))

    def test_config_rejects_a_non_bool(self):
        with self.assertRaises(ValueError):
            config.validate(dict(CFG, design_briefs="yes"))
        config.validate(dict(CFG, design_briefs=True))

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = cli.main(["design", "reaches-owner", *argv])
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue().splitlines()

    def test_reaches_owner_threshold(self):
        for argv, want in ((("--files", "3", "--risk", "", "--reversible", "yes"), "no"),
                           (("--files", "20", "--risk", "judges,big", "--reversible", "yes"), "no"),
                           (("--files", "21", "--risk", "", "--reversible", "yes"), "yes"),
                           (("--files", "1", "--risk", "public", "--reversible", "yes"), "yes"),
                           (("--files", "1", "--risk", "data", "--reversible", "yes"), "yes"),
                           (("--files", "1", "--risk", "", "--reversible", "no"), "yes")):
            rc, lines = self.run_cli(*argv)
            self.assertEqual((rc, lines[0], len(lines)), (0, want, 2), argv)

    def test_reaches_owner_bad_input_exits_2(self):
        for argv in (("--files", "x", "--risk", "", "--reversible", "yes"),
                     ("--files", "1", "--risk", "bogus", "--reversible", "yes"),
                     ("--files", "-1", "--risk", "", "--reversible", "yes")):
            self.assertEqual(self.run_cli(*argv)[0], 2, argv)


if __name__ == "__main__":
    unittest.main()
