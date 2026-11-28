"""Lane holds in `capsule status`: a lane (epic branch) held by an unfinished card shows the
holder and how many ready cards wait. The alert on a stuck hold moved to the stall supervisor
(capsule/stall.py); epic_clean no longer opens an incident or pages by itself.
Temp boards only, built with the repo's own schema helper; the hold rule itself is pinned
elsewhere (`epic_clean.holders`) and must not change."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, epic_clean, status_cli, statusview

NOW = 1_800_000_000
BRANCH = "epic/lane-a"
CFG = {"epics": [], "board": "bx"}


class LaneCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        d = self.home / "boards" / "bx"
        d.mkdir(parents=True)
        db.create_board(d / "kanban.db")
        self.conn = db.connect(d / "kanban.db")
        self.addCleanup(self.conn.close)
        self.work = Path(tempfile.mkdtemp())

    def card(self, title, status="ready", branch=BRANCH, at=NOW - 10 * 3600):
        tid = db.new_task(self.conn, title=title, assignee="dev-backend", status=status,
                          branch=branch, now=at)
        return tid

    def add_run(self, tid, ended=None, started=NOW - 5 * 3600):
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at)"
            " VALUES (?,'implementer',?,?,?)",
            (tid, "done" if ended else "running", started, ended))

    def stuck(self, title="Stuck card", status="blocked", ended=NOW - 2 * 3600, ready=3):
        holder = self.card(title, status=status)
        self.add_run(holder, ended=ended)
        for i in range(ready):
            self.card(f"Waiting {i}")
        return holder

    def two_lanes(self, ended_b=NOW - 2 * 3600):
        self.stuck()
        other = self.card("Other stuck", status="blocked", branch="epic/lane-b")
        self.add_run(other, ended=ended_b)
        self.card("Other waiting", branch="epic/lane-b")

    def sweep(self, now=NOW):
        epic_clean.clean_idle(self.conn, CFG, self.work, now=now)

    def assert_sweep_is_silent(self):
        """The sweep opens no incident and writes no outbox row for a hold (the stall
        supervisor owns the alert)."""
        self.sweep()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def snap(self):
        return statusview.snapshot(self.home, now=NOW, boards=["bx"],
                                   events_path=self.home / "events.jsonl")


class StatusNamesTheHold(LaneCase):
    def test_lane_names_holder_and_ready_count_in_json_and_on_screen(self):
        self.stuck()
        snap = self.snap()
        (lane,) = snap["lanes"]
        self.assertEqual((lane["name"], lane["waiting"], lane["branch"]),
                         ("Stuck card", 3, BRANCH))
        text = status_cli.render({**snap, "redesign": {}})
        line = next(x for x in text.splitlines() if "Stuck card" in x and "lane" in x.lower())
        self.assertIn("3 ready", line)

    def test_running_holder_is_named_too(self):
        holder = self.card("Busy card", status="running")
        self.add_run(holder)
        self.card("Behind it")
        self.card("Only ready", branch="epic/lane-c")      # no holder: not listed
        (lane,) = self.snap()["lanes"]
        self.assertEqual((lane["name"], lane["waiting"]), ("Busy card", 1))



class ReportsWithoutLifting(LaneCase):
    def test_holders_and_cards_are_unchanged_by_the_sweep(self):
        holder = self.stuck()
        before = epic_clean.holders(self.conn, BRANCH)
        rows = [tuple(r) for r in self.conn.execute("SELECT id, status FROM tasks ORDER BY id")]
        self.sweep()
        self.snap()
        self.assertEqual(epic_clean.holders(self.conn, BRANCH), before)
        self.assertIn(holder, before)
        self.assertEqual([tuple(r) for r in
                          self.conn.execute("SELECT id, status FROM tasks ORDER BY id")], rows)



class CapsGrowth(LaneCase):
    def many(self, n, title="Held"):
        for i in range(n):
            tid = self.card(f"{title} {i}", status="blocked")
            self.add_run(tid, ended=NOW - 2 * 3600)
        self.card("Waiting")

    def test_long_titles_are_trimmed_to_80_characters(self):
        self.stuck(title="x" * 300)
        line = next(x for x in status_cli.render({**self.snap(), "redesign": {}}).splitlines()
                    if "held by" in x)
        self.assertNotIn("x" * 81, line)

    def test_the_screen_names_five_holders_then_a_count(self):
        self.many(8)
        self.assert_sweep_is_silent()
        line = next(x for x in status_cli.render({**self.snap(), "redesign": {}}).splitlines()
                    if "held by" in x)
        self.assertEqual(line.count("Held "), 5)
        self.assertIn("+3 more", line)


class OneBadLaneIsNotABadBoard(LaneCase):
    def break_lane_b(self):
        real = epic_clean._lane

        def fake(conn, branch, ids, now):
            if branch == "epic/lane-b":
                raise KeyError("bad row")
            return real(conn, branch, ids, now)
        return mock.patch.object(epic_clean, "_lane", fake)

    def test_only_the_unreadable_lane_is_marked(self):
        self.two_lanes()
        with self.break_lane_b():
            snap = self.snap()
        self.assertFalse(any("cannot be opened" in a["what"] for a in snap["attention"]["open"]))
        by_branch = {lane["branch"]: lane for lane in snap["lanes"]}
        self.assertEqual(by_branch[BRANCH]["name"], "Stuck card")
        self.assertTrue(by_branch["epic/lane-b"]["unreadable"])
        self.assertNotIn("unreadable", by_branch[BRANCH])


    def test_garbage_in_one_lanes_run_time_marks_only_that_lane(self):
        self.two_lanes(ended_b="garbage")
        self.conn.execute("UPDATE tasks SET title=? WHERE title='Stuck card'", ("w" * 120,))
        snap = self.snap()
        self.assertFalse(any("cannot be opened" in a["what"] for a in snap["attention"]["open"]))
        by_branch = {lane["branch"]: lane for lane in snap["lanes"]}
        self.assertTrue(by_branch["epic/lane-b"]["unreadable"])
        self.assertTrue(any("epic/lane-b" in a["what"] and "ValueError" in a["what"]
                            for a in snap["attention"]["open"]))
        self.assertEqual(len(by_branch[BRANCH]["holders"][0]["name"]), 120)   # JSON: full title
        text = status_cli.render({**snap, "redesign": {}})
        held = next(x for x in text.splitlines() if "held by" in x)
        self.assertIn("w" * 80, held)
        self.assertNotIn("w" * 81, held)
        self.assertTrue(any("epic/lane-b" in x and "cannot be read" in x
                            for x in text.splitlines()))

    def test_screen_says_which_lane_cannot_be_read(self):
        self.two_lanes()
        with self.break_lane_b():
            text = status_cli.render({**self.snap(), "redesign": {}})
        self.assertIn("Stuck card", text)
        self.assertTrue(any("epic/lane-b" in x and "cannot be read" in x
                            for x in text.splitlines()))


class NothingFailsSilently(LaneCase):
    def test_an_unreadable_lane_logs_its_name_and_the_exception_type(self):
        self.stuck()
        self.assert_sweep_is_silent()
        err = io.StringIO()
        with mock.patch.object(epic_clean, "_lane", side_effect=KeyError("bad row")), \
                contextlib.redirect_stderr(err):
            self.snap()
        self.assertIn(BRANCH, err.getvalue())
        self.assertIn("KeyError", err.getvalue())

    def test_a_long_title_keeps_its_number_and_ready_marker_on_the_screen(self):
        self.stuck(title="y" * 300)
        ready_holder = self.card("z" * 300, status="ready")
        self.add_run(ready_holder, ended=NOW - 2 * 3600)
        line = next(x for x in status_cli.render({**self.snap(), "redesign": {}}).splitlines()
                    if "held by" in x)
        self.assertNotIn("y" * 81, line)
        self.assertRegex(line, r"y+ #\d+, z+ #\d+ \(ready\)")


if __name__ == "__main__":
    unittest.main()
