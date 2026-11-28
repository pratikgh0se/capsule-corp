# mutation-check: targets-predate-range b957739c9 -- renames only, no behaviour pinned ::
#   YourTime.test_terminal_hours_sum_recorded_human_minutes
"""M007/M201: the `## YOUR TIME` section of `capsule report --md`. Oracle 1: hand arithmetic on a
fixture board. Oracle 2: the same numbers from the human minutes the report already carries."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import db, human, human_minutes, paths, report, spool, touches

DAY = 86400
NOW = 100 * DAY
NO_VAULT = "/nonexistent-vault"


class YourTime(unittest.TestCase):
    def setUp(self):    # never the real maintenance state
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        old = os.environ.get("AGENT_OPS_STATE_DIR")
        os.environ["AGENT_OPS_STATE_DIR"] = str(self.state)
        self.addCleanup(lambda: os.environ.__setitem__("AGENT_OPS_STATE_DIR", old) if old
                        else os.environ.pop("AGENT_OPS_STATE_DIR", None))

    def batch(self, rows):
        (self.state / "brief-batch.json").write_text(json.dumps(rows), encoding="utf-8")

    def board(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        db.create_board(paths.board_db("t", home))
        conn = db.connect(paths.board_db("t", home))
        self.tid = db.new_task(conn, title="c", now=1)
        conn.commit()
        return home, conn

    def section(self, home, conn=None):
        if conn:
            conn.commit()
            conn.close()
        doc = touches.evaluate(home, "t", since=0, now=NOW, vault=NO_VAULT)
        lines = report.render_md(doc).splitlines()
        i = lines.index("## YOUR TIME")
        return lines[i + 1:i + 6], lines[i + 6], doc

    def approve(self, conn, asked, at, **extra):
        tid = db.new_task(conn, title="c", now=1)    # one card per approval: its own request
        db.event(conn, tid, "decision_requested", {"question": "q"}, now=asked)
        db.event(conn, tid, "intervention", {"kind": "approve", "at": at, **extra}, now=at)

    def test_empty_board_every_line_says_not_enough_data(self):
        home, conn = self.board()
        out, after, _ = self.section(home, conn)
        self.assertEqual(out, ["- Decisions per day by class: not enough data",
                               "- Approvals per day: not enough data",
                               "- Terminal hours: not enough data",
                               "- Overflow: not enough data",
                               "- Slowest: not enough data"])
        self.assertEqual(after, "")

    def test_approvals_per_day_and_median_wait(self):
        home, conn = self.board()
        t = NOW - 2 * DAY
        self.approve(conn, t, t + 3600)          # waited 1h
        self.approve(conn, t + 10, t + 10 + 3 * 3600 + 600)   # waited 3h 10m
        self.approve(conn, t + 20, t + 20 + 8 * 3600)    # 8h; median is 3h 10m
        self.approve(conn, NOW - 9 * DAY, NOW - 9 * DAY + 60)    # outside the window
        self.approve(conn, t, t + 5, author="cos")               # the chief of staff's: not his
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[1], "- Approvals per day: 0.4, median wait 3h 10m")   # 3 / 7

    def test_even_count_median_is_the_mean_of_the_middle_two(self):
        home, conn = self.board()
        t = NOW - DAY
        self.approve(conn, t, t + 3600)
        self.approve(conn, t, t + 2 * 3600)
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[1], "- Approvals per day: 0.3, median wait 1h 30m")

    def test_request_in_the_same_second_is_not_a_wait(self):
        home, conn = self.board()
        self.approve(conn, NOW - 100, NOW - 100)
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[1], "- Approvals per day: 0.1, median wait not enough data")

    def test_window_start_is_inclusive(self):
        home, conn = self.board()
        self.approve(conn, NOW - 7 * DAY - 60, NOW - 7 * DAY)         # exactly 7 days ago: in
        self.approve(conn, NOW - 7 * DAY - 120, NOW - 7 * DAY - 1)    # one second older: out
        db.event(conn, self.tid, "decision_requested", {"scope": "text"}, now=NOW - 7 * DAY)
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[1], "- Approvals per day: 0.1, median wait 0h 01m")
        self.assertEqual(out[0], "- Decisions per day by class: text 0.1")

    def test_malformed_rows_are_skipped_not_fatal(self):
        home, conn = self.board()
        for kind in ("intervention", "decision_requested"):
            for bad in ("[1, 2]", '"str"', "{oops", "7", '{"kind": "approve", "at": "abc"}',
                        '{"kind": "approve", "at": [1]}'):
                conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at)"
                             " VALUES (?,?,?,?)", (self.tid, kind, bad, NOW - 50))
        self.batch(["x", 3, {"kind": "waiting"}, {"kind": "waiting", "ts": "soon"},
                    {"kind": "waiting", "ts": NOW - 3600}])
        out, _, doc = self.section(home, conn)
        self.assertEqual(out[1], "- Approvals per day: not enough data")
        self.assertEqual(out[0], "- Decisions per day by class: unscoped 0.9")   # 6 events / 7
        self.assertEqual(out[3], "- Overflow: 1 message, oldest 1h 00m")
        self.assertIn("your_time", doc)

    def test_malformed_failure_and_amend_payloads_do_not_crash_the_report(self):
        home, conn = self.board()
        for kind in report.FAILURE_EVENTS + ("contract_amended",):
            for bad in ("[1, 2]", '"s"', "{oops"):
                conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at)"
                             " VALUES (?,?,?,?)", (self.tid, kind, bad, NOW - 50))
        db.event(conn, self.tid, "failed", {"run_id": "r1"}, now=NOW - 40)
        conn.commit()
        conn.close()
        doc = touches.evaluate(home, "t", since=0, now=NOW, vault=NO_VAULT)
        self.assertEqual([f["run_id"] for f in doc["cards"][0]["failure_events"]], ["r1"])
        self.assertIn("## YOUR TIME", report.render_md(doc))

    def test_overflow_not_a_list_is_no_data(self):
        home, conn = self.board()
        (self.state / "brief-batch.json").write_text('{"a": 1}', encoding="utf-8")
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[3], "- Overflow: not enough data")

    def test_approval_with_no_recorded_request_has_no_wait(self):
        home, conn = self.board()
        db.event(conn, self.tid, "intervention", {"kind": "accept", "at": NOW - 5}, now=NOW - 5)
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[1], "- Approvals per day: 0.1, median wait not enough data")

    def test_terminal_hours_sum_recorded_human_minutes(self):
        home, conn = self.board()
        conn.commit()
        for i, kind in enumerate(["answer"] * 9 + ["approve"] * 4 + ["close"] * 5):
            human.write(home, "t", self.tid, kind, "x", now=NOW - DAY + i, author="owner")
        human.write(home, "t", self.tid, "answer", "x", now=NOW - 20 * DAY, author="owner")
        out, _, doc = self.section(home, conn)
        docs = [d for v in spool.human_by_task(home, "t", NOW - 7 * DAY).values() for d in v]
        want = human_minutes.measure(home, "t", docs, NOW - 7 * DAY, NOW)["human_minutes"] / 60
        self.assertAlmostEqual(want, 40 / 60)     # hand sum: 9*3 + 4*2 + 5*1 minutes
        self.assertEqual(out[2], f"- Terminal hours: {want:.1f}")

    def test_zero_prints_only_when_data_exists(self):
        home, conn = self.board()
        conn.commit()
        human.write(home, "t", self.tid, "answer", "x", now=NOW - DAY, author="cos")
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[2], "- Terminal hours: 0.0")    # a file exists, none of it his

    def test_deterministic_for_fixed_board_and_time(self):
        home, conn = self.board()
        self.approve(conn, NOW - 100, NOW - 50)
        a = self.section(home, conn)[0]
        self.assertEqual(a, self.section(home)[0])

    def test_decisions_per_day_grouped_by_scope_alphabetically(self):
        home, conn = self.board()
        for scope, n in (("product", 1), ("harness", 2), (None, 4)):
            for i in range(n):
                payload = {"question": "q", **({"scope": scope} if scope else {})}
                db.event(conn, self.tid, "decision_requested", payload, now=NOW - 100 - i)
        db.event(conn, self.tid, "decision_requested", {"scope": "text"}, now=NOW - 9 * DAY)
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[0], "- Decisions per day by class: harness 0.3, product 0.1,"
                                 " unscoped 0.6")

    def test_overflow_counts_waiting_rows_and_names_the_oldest(self):
        home, conn = self.board()
        age = DAY + 3 * 3600 + 60
        self.batch([{"ts": NOW - age, "kind": "waiting"}, {"ts": NOW - 60, "kind": "waiting"},
                    {"ts": NOW - 60, "kind": "other"}, {"ts": NOW - 30 * DAY, "kind": "waiting"}])
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[3], "- Overflow: 2 messages, oldest 1d 3h")

    def request(self, conn, at, status="open", kind="decision_requested"):
        tid = db.new_task(conn, title="c", now=1)
        db.event(conn, tid, kind, {"question": "q"} if kind == "decision_requested"
                 else {"reason": "r"}, now=at)
        if status != "open":
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        return tid

    def test_slowest_is_the_longest_wait_answered_or_open(self):
        home, conn = self.board()
        self.approve(conn, NOW - 3 * DAY, NOW - 3 * DAY + 3600)    # answered, 1h
        open_id = self.request(conn, NOW - DAY - 3 * 3600)         # still open: 1d 3h so far
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[4], f"- Slowest: {open_id}, open 1d 3h")

    def test_slowest_answered_beats_a_younger_open_request(self):
        home, conn = self.board()
        t = NOW - 2 * DAY
        self.approve(conn, t, t + 5 * 3600)                         # answered, 5h
        self.request(conn, NOW - 3600, kind="blocked")              # open, 1h
        out, _, _ = self.section(home, conn)
        self.assertRegex(out[4], r"^- Slowest: \S+, answered after 5h 00m$")

    def test_slowest_skips_a_request_closed_without_an_approval(self):
        home, conn = self.board()
        for close, payload in (("unblocked", {"status": "ready"}), ("failure_reset", {}),
                               ("request_review", {"by": "accept"})):
            tid = self.request(conn, NOW - 2 * DAY)
            db.event(conn, tid, close, payload, now=NOW - DAY)       # closed, card running again
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[4], "- Slowest: not enough data")

    def test_slowest_ignores_done_cards_and_answered_requests(self):
        home, conn = self.board()
        self.request(conn, NOW - 2 * DAY, status="done")            # terminal: not waiting
        self.request(conn, NOW - 2 * DAY, status="archived")
        self.approve(conn, NOW - 9 * DAY, NOW - 9 * DAY + 60)       # answered outside the window
        self.approve(conn, NOW - 2 * DAY, NOW - 2 * DAY + 5, author="cos")   # not his
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[4], "- Slowest: not enough data")

    def test_overflow_empty_batch_is_a_real_zero(self):
        home, conn = self.board()
        self.batch([])
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[3], "- Overflow: 0 messages, none pending")

    def test_overflow_unreadable_file_is_no_data_and_does_not_crash(self):
        home, conn = self.board()
        (self.state / "brief-batch.json").write_text("{not json", encoding="utf-8")
        out, _, _ = self.section(home, conn)
        self.assertEqual(out[3], "- Overflow: not enough data")


if __name__ == "__main__":
    unittest.main()
