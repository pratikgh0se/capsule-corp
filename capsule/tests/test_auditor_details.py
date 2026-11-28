"""The audit packet's fact lists: `flaky_tests_detail` and `stuck_cards_detail`."""
import unittest

from capsule import auditor, db
from capsule.tests.test_auditor import E, IN, OLD, card, make_board, packet, sql


def flaky(path, details, now=IN):
    conn = db.connect(path)
    t = card(path)
    db.event(conn, t, "landing_checked", {"verdict": "ok"}, now=OLD)
    for d in details:
        db.event(conn, t, "landing_flaky", {"detail": d}, now=now)
    conn.close()


class FlakyDetail(unittest.TestCase):
    def test_counts_per_test_sorted_and_windowed(self):
        home, path = make_board()
        flaky(path, [f"red\nFAIL: t_a (pkg.T.t_a)\nFAIL: t_b (pkg.T.t_b)\nRan {n} tests"
                     for n in (5, 6)])
        flaky(path, ["red\nFAIL: t_b (pkg.T.t_b)"])
        flaky(path, ["FAIL: t_old (pkg.T.t_old)"], now=OLD)
        got = packet(home)["flaky_tests_detail"]
        self.assertEqual(got, [{"test": "pkg.T.t_b", "count": 3, "source": "landing"},
                               {"test": "pkg.T.t_a", "count": 2, "source": "landing"}])

    def test_a_test_named_again_in_the_tail_counts_once_per_event(self):
        home, path = make_board()
        flaky(path, ["red\nFAIL: t_a (pkg.T.t_a)\n--- tail ---\nFAIL: t_a (pkg.T.t_a)\nboom"])
        got = packet(home)["flaky_tests_detail"]
        self.assertEqual(got, [{"test": "pkg.T.t_a", "count": 1, "source": "landing"}])

    def test_no_id_falls_back_to_the_first_line_cut_to_300(self):
        home, path = make_board()
        flaky(path, ["gate: " + "x" * 400 + "\nmore", "gate: " + "x" * 400 + "\nother", "", "  "])
        conn = db.connect(path)
        db.event(conn, card(path), "landing_flaky", {"detail": 5}, now=IN)
        conn.close()
        got = packet(home)["flaky_tests_detail"]
        self.assertEqual(got, [{"test": ("gate: " + "x" * 400)[:300], "count": 2,
                                "source": "landing"}])

    def test_a_malformed_payload_leaves_the_key_out_and_names_it_missing(self):
        home, path = make_board()
        flaky(path, ["FAIL: a (p.T.a)"])
        sql(path, [("UPDATE task_events SET payload='{bad' WHERE kind='landing_flaky'", ())])
        p = packet(home)
        self.assertNotIn("flaky_tests_detail", p)
        self.assertIn("flaky_tests_detail", p["missing"])
        self.assertEqual(p["metrics"]["flaky_tests"], 1)

    def test_capped_at_40(self):
        home, path = make_board()
        flaky(path, [f"FAIL: t{i} (pkg.T.t{i})" for i in range(45)])
        self.assertEqual(len(packet(home)["flaky_tests_detail"]), auditor.DETAIL_CAP)

    def test_absent_when_the_lander_never_ran(self):
        home, _path = make_board()
        p = packet(home)
        self.assertNotIn("flaky_tests_detail", p)
        self.assertIn("flaky_tests", p["missing"])


class StuckDetail(unittest.TestCase):
    def test_idle_hours_sorted_longest_first(self):
        home, path = make_board()
        a = card(path, "running", created=E - 3 * 86400)
        b = card(path, "ready", created=E - 50 * 3600 - 1900)
        card(path, "ready", created=E - 47 * 3600)
        card(path, "done", created=E - 9 * 86400, completed=E - 8 * 86400)
        self.assertEqual(packet(home)["stuck_cards_detail"],
                         [{"task_id": a, "status": "running", "idle_h": 72.0},
                          {"task_id": b, "status": "ready", "idle_h": 50.5}])

    def test_capped_at_40_while_the_counter_stays_full(self):
        home, path = make_board()
        for _ in range(45):
            card(path, "ready", created=E - 3 * 86400)
        p = packet(home)
        self.assertEqual((len(p["stuck_cards_detail"]), p["metrics"]["stuck_cards"]),
                         (auditor.DETAIL_CAP, 45))

    def test_absent_when_the_board_is_unreadable(self):
        home, path = make_board()
        path.write_bytes(b"not a database" * 100)
        p = packet(home)
        self.assertNotIn("stuck_cards_detail", p)
        self.assertIn("stuck_cards", p["missing"])


if __name__ == "__main__":
    unittest.main()
