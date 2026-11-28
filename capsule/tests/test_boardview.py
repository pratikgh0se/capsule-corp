"""`capsule/boardview.py`: the read-only `capsule board` screens (visibility spec card 10)."""
import io
import re
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from capsule import boardview, db, statusview, unblocked
from capsule.__main__ import main

NOW = 1_800_000_000
OLD = NOW - 5 * 3600
ID_RE = re.compile(r"t_[0-9a-f]{8}|inc_|" + re.escape(str(Path.home())))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        d = self.home / "boards" / "bx"
        d.mkdir(parents=True)
        db.create_board(d / "kanban.db")
        self.conn = db.connect(d / "kanban.db")
        self.addCleanup(self.conn.close)
        self.task("Wait for review", "review", short_name="Review pending")
        self.task("Needs a ruling", "blocked", kind="needs_input", short_name="Ruling please")
        self.task("Dependent card", "blocked", kind="dependency")
        tid = self.task("Build the thing", "running", short_name="Build thing")
        cur = self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, model, last_heartbeat_at)"
            " VALUES (?,?,'running',?,?,?)",
            (tid, "implementer", NOW - 600, "claude-sonnet-5-5", NOW))
        self.conn.execute("UPDATE tasks SET current_run_id=?, branch_name='keeper/x' WHERE id=?",
                          (cur.lastrowid, tid))
        db.event(self.conn, tid, "claimed", now=NOW - 300)
        self.conn.commit()

    def task(self, title, status, kind=None, short_name=None):
        tid = db.new_task(self.conn, title=title, assignee="dev", status=status,
                          short_name=short_name, now=OLD)
        self.conn.execute("UPDATE tasks SET block_kind=? WHERE id=?", (kind, tid))
        return tid

    def tasks(self):
        ro = boardview.open_board(self.home, "bx")
        self.addCleanup(ro.close)
        return boardview.load(ro, NOW)


class Screens(Fixture):
    def test_groups_follow_statusview_classifiers(self):
        got = {t["name"]: t["group"] for t in self.tasks()}
        snap = statusview.snapshot(self.home, now=NOW, boards=["bx"])
        for sect, group in (("needs_you", "needs"), ("running", "running"), ("blocked", "blocked")):
            for item in snap[sect]:
                self.assertEqual(got[item["name"]], group)
        self.assertEqual(got["Review pending"], "waiting")

    def test_list_lines_have_name_number_state_age_and_no_ids(self):
        lines = boardview.render_list(self.tasks(), NOW, selected=0)
        text = "\n".join(lines)
        self.assertRegex(text, r"🙋 Ruling please #\d+  needs you  5h")
        self.assertIn("▶️ Build thing", text)
        self.assertIsNone(ID_RE.search(text))
        self.assertTrue(lines[1].startswith(">"))

    def test_detail_has_title_state_events_branch_and_no_ids(self):
        t = next(x for x in self.tasks() if x["name"] == "Build thing")
        text = "\n".join(boardview._render_detail(t, NOW))
        for want in ("Build the thing", "state: running", "running as builder",
                     "branch: keeper/x", "5m ago · claimed", "Esc/b back"):
            self.assertIn(want, text)
        self.assertIsNone(ID_RE.search(text))

    def test_once_prints_the_list_for_a_fixture_board(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["board", "bx", "--once", "--capsule-home", str(self.home)])
        self.assertEqual(rc, 0)
        self.assertIn("NEEDS YOU (1)", buf.getvalue())
        self.assertIn("BLOCKED (1)", buf.getvalue())

    def test_empty_board_says_so(self):
        self.assertEqual(boardview.render_list([], NOW), ["  nothing open on this board"])

    def test_window_keeps_selection_visible_with_help_last(self):
        tasks = [{"group": "waiting", "number": i + 1, "name": f"Task {i}", "state": "ready",
                  "since": OLD} for i in range(31)]
        lines = boardview._window(boardview.render_list(tasks, NOW, selected=30), 10)
        self.assertEqual(len(lines), 9)
        self.assertTrue(lines[-2].startswith("> ") and "#31" in lines[-2])
        self.assertEqual(lines[-1], boardview.HELP)
        top = boardview._window(boardview.render_list(tasks, NOW, selected=0), 10)
        self.assertIn("#1", top[1])

    def test_board_is_read_only(self):
        ro = boardview.open_board(self.home, "bx")
        self.addCleanup(ro.close)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("UPDATE tasks SET status='done'")


class DefaultBoard(unittest.TestCase):
    """No board argument: the first board of config `unblocked.boards`, else the one
    shipped default (no private board name in code)."""

    def run_default(self, home):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = main(["board", "--once", "--capsule-home", str(home)])
        return rc, err.getvalue()

    def test_configured_first_board_is_the_default(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text('{"unblocked": {"boards": ["zz-board", "other"]}}')
        rc, err = self.run_default(home)
        self.assertEqual(rc, 2)
        self.assertIn("no board named zz-board", err)

    def test_no_config_falls_back_to_the_shipped_default_only(self):
        home = Path(tempfile.mkdtemp())
        rc, err = self.run_default(home)
        self.assertEqual(rc, 2)
        self.assertIn("no board named capsule-corp", err)
        self.assertEqual(unblocked.DEFAULT_BOARDS, ("capsule-corp",))


if __name__ == "__main__":
    unittest.main()
