import argparse
import atexit
import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import auditor, db, human, paths

BOARD = "aud"
S, E = 1_000_000, 1_000_000 + 7 * 86400
IN = S + 100        # inside the window
OLD = S - 10 * 86400


def tmp():
    d = Path(tempfile.mkdtemp(prefix="aud"))
    atexit.register(shutil.rmtree, d, True)
    return d


def make_board():
    home = tmp()
    path = paths.board_db(BOARD, home)
    db.create_board(path)
    return home, path


def run(home, *extra, window=(S, E)):
    parser = argparse.ArgumentParser()
    auditor.add_parser(parser.add_subparsers(dest="cmd"))
    argv = ["auditor", "packet", "--board", BOARD, "--capsule-home", str(home), *extra]
    if window:
        argv += ["--window", str(window[0]), str(window[1])]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = auditor.handle(parser.parse_args(argv), parser)
    return rc, out.getvalue()


def packet(home, *extra, **kw):
    rc, text = run(home, *extra, **kw)
    assert rc == 0, text
    return json.loads(text)


def sql(path, statements):
    conn = db.connect(path)
    for stmt, params in statements:
        conn.execute(stmt, params)
    conn.commit()
    conn.close()


def card(path, status="ready", created=OLD, completed=None):
    conn = db.connect(path)
    tid = db.new_task(conn, title="c", now=created)
    conn.execute("UPDATE tasks SET status=?, completed_at=? WHERE id=?", (status, completed, tid))
    conn.commit()
    conn.close()
    return tid


def add_run(path, tid, started=IN, profile="implementer", outcome="completed", tok=None):
    t_in, t_out, t_cw = tok or (None, None, None)
    sql(path, [("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, tokens_in,"
                " tokens_out, tokens_cache_write) VALUES (?,?,'done',?,?,?,?,?)",
                (tid, profile, outcome, started, t_in, t_out, t_cw))])


def incident(path, iid, opened, fp="fp1", repeat_of=None, status="open", closed=None):
    kind = "repeat" if repeat_of else "crash"
    sql(path, [("INSERT INTO incidents (id, board, kind, opened_at, fingerprint, status, closed_at,"
                " repeat_of) VALUES (?,?,?,?,?,?,?,?)",
                (iid, BOARD, kind, opened, fp, status, closed, repeat_of))])


class Metrics(unittest.TestCase):
    def test_fresh_board_missing_not_zero(self):
        home, _ = make_board()
        p = packet(home)
        self.assertEqual(p["schema"], "audit_packet.v1")
        self.assertEqual(p["missing"],
                         ["cost_per_card", "flaky_tests", "interventions", "tokens"])
        self.assertEqual(p["metrics"], {"landed": 0, "review_rounds": 0, "incidents": 0,
                                        "stuck_cards": 0})
        self.assertEqual((p["trigger"], p["worse"], p["prior_window"], p["repeats"]),
                         ("weekly", [], None, []))
        self.assertEqual(p["window"], [S, E])

    def test_unreadable_or_absent_board_misses_everything(self):
        home = tmp()
        p = packet(home)
        self.assertEqual(p["metrics"], {})
        self.assertEqual(sorted(p["missing"]), sorted(
            ["landed", "tokens", "cost_per_card", "review_rounds", "interventions", "incidents",
             "flaky_tests", "stuck_cards", "repeats"]))
        self.assertFalse(paths.board_db(BOARD, home).exists())

    def test_landed_cost_and_tokens(self):
        home, path = make_board()
        a = card(path, "done", completed=IN)
        card(path, "done", completed=IN + 5)
        card(path, "done", completed=OLD)          # landed before the window
        card(path, "done", completed=E)            # the window end is exclusive
        card(path, "ready")
        add_run(path, a, tok=(100, 50, 25))
        add_run(path, a, tok=(10, None, None))
        add_run(path, a, started=OLD, tok=(999, 1, 1))     # outside the window
        m = packet(home)["metrics"]
        self.assertEqual((m["landed"], m["tokens"]), (2, 185))
        self.assertEqual(m["cost_per_card"], 92.5)

    def test_quiet_week_is_zero_once_a_token_was_ever_written(self):
        home, path = make_board()
        add_run(path, card(path), started=OLD, tok=(7, 1, 1))
        p = packet(home)
        self.assertEqual(p["metrics"]["tokens"], 0)
        self.assertIsNone(p["metrics"]["cost_per_card"])

    def test_quiet_week_without_any_token_ever_is_missing(self):
        home, path = make_board()
        add_run(path, card(path), started=OLD, tok=None)
        card(path, "done", completed=IN)
        p = packet(home)
        self.assertIn("tokens", p["missing"])
        self.assertIn("cost_per_card", p["missing"])
        self.assertNotIn("cost_per_card", p["metrics"])

    def test_runs_without_any_token_writer_are_missing(self):
        home, path = make_board()
        add_run(path, card(path), tok=None)
        p = packet(home)
        self.assertIn("tokens", p["missing"])
        self.assertIn("cost_per_card", p["missing"])
        self.assertNotIn("tokens", p["metrics"])

    def test_review_rounds_count_implementer_runs_that_open_a_round(self):
        home, path = make_board()
        t = card(path)
        add_run(path, t)                                   # completed: a round
        add_run(path, t, outcome="failed")                 # no round
        add_run(path, t, profile="reviewer")               # not an implementer
        add_run(path, t, started=OLD)                      # outside the window
        add_run(path, t, outcome="blocked")
        conn = db.connect(path)
        rid = conn.execute("SELECT MAX(id) FROM task_runs").fetchone()[0]
        db.event(conn, t, "request_review", {"by": "human"}, run_pk=rid, now=IN)
        conn.close()
        self.assertEqual(packet(home)["metrics"]["review_rounds"], 2)

    def test_interventions_zero_when_dir_exists_missing_when_not(self):
        home, path = make_board()
        t = card(path)
        self.assertIn("interventions", packet(home)["missing"])
        human.write(home, BOARD, t, "comment", "in", now=IN)
        human.write(home, BOARD, t, "comment", "old", now=OLD)
        human.write(home, BOARD, t, "comment", "late", now=E + 1)
        self.assertEqual(packet(home)["metrics"]["interventions"], 1)
        self.assertEqual(packet(home, window=(E, E + 10))["metrics"]["interventions"], 1)
        self.assertEqual(packet(home, window=(E + 10 ** 7, E + 2 * 10 ** 7))["metrics"]
                         ["interventions"], 0)

    def test_one_bad_human_file_is_skipped(self):
        home, path = make_board()
        t = card(path)
        human.write(home, BOARD, t, "comment", "in", now=IN)
        d = paths.board_db(BOARD, home).parent / "human"
        (d / "bad.json").write_text('{"task_id": "x", "at": "soon"}')
        (d / "list.json").write_text("[1]")
        (d / "junk.json").write_text("{")
        p = packet(home)
        self.assertEqual(p["metrics"]["interventions"], 1)
        self.assertNotIn("interventions", p["missing"])

    def test_incidents_opened_in_window(self):
        home, path = make_board()
        incident(path, "inc_00000001", IN, fp="a")
        incident(path, "inc_00000002", OLD, fp="b")
        self.assertEqual(packet(home)["metrics"]["incidents"], 1)

    def test_flaky_zero_once_the_lander_ran_and_counts_events(self):
        home, path = make_board()
        t = card(path)
        conn = db.connect(path)
        db.event(conn, t, "landing_checked", {"verdict": "ok"}, now=OLD)
        conn.close()
        self.assertEqual(packet(home)["metrics"]["flaky_tests"], 0)
        conn = db.connect(path)
        db.event(conn, t, "landing_flaky", {"detail": "x"}, now=IN)
        db.event(conn, t, "landing_flaky", {"detail": "y"}, now=IN + 1)
        db.event(conn, t, "landing_flaky", {"detail": "old"}, now=OLD)
        conn.close()
        p = packet(home)
        self.assertEqual(p["metrics"]["flaky_tests"], 2)
        self.assertNotIn("flaky_tests", p["missing"])

    def test_stuck_cards_more_than_48h_in_a_live_status(self):
        home, path = make_board()
        end = E
        old = card(path, "running", created=end - 3 * 86400)              # stuck
        busy = card(path, "running", created=end - 3 * 86400)             # a recent event
        card(path, "ready", created=end - 47 * 3600)                      # under 48 h
        card(path, "ready", created=end - 48 * 3600)                      # exactly 48 h
        card(path, "done", created=end - 9 * 86400, completed=end - 8 * 86400)
        card(path, "ready", created=end + 5)                              # born after the end
        conn = db.connect(path)
        db.event(conn, busy, "claim_extended", {"reason": "x"}, now=end - 3600)
        db.event(conn, old, "claim_extended", {"reason": "x"}, now=end + 3600)   # after the end
        conn.close()
        self.assertEqual(packet(home)["metrics"]["stuck_cards"], 1)


class Repeats(unittest.TestCase):
    def test_repeats_listed_with_their_original(self):
        home, path = make_board()
        incident(path, "inc_0000000a", OLD, fp="x", status="closed", closed=OLD + 100)
        incident(path, "inc_0000000b", IN, fp="x", repeat_of="inc_0000000a")
        incident(path, "inc_0000000c", IN + 1, fp="y")
        incident(path, "inc_0000000d", OLD + 5, fp="z", repeat_of="inc_0000000a")   # old repeat
        p = packet(home)
        self.assertEqual(p["repeats"], [{"incident": "inc_0000000b", "repeat_of": "inc_0000000a",
                                         "kind": "repeat"}])
        self.assertEqual(p["metrics"]["incidents"], 2)


class Worse(unittest.TestCase):
    def prior_file(self, metrics, window=(S - 7 * 86400, S)):
        f = tmp() / "prior.json"
        f.write_text(json.dumps({"schema": "audit_packet.v1", "window": list(window),
                                 "metrics": metrics}))
        return str(f)

    def test_higher_is_worse_but_landed_lower_is_worse(self):
        home, path = make_board()
        a = card(path, "done", completed=IN)
        add_run(path, a, tok=(100, 0, 0))
        incident(path, "inc_00000001", IN)
        prior = self.prior_file({"landed": 3, "tokens": 50, "cost_per_card": 10.0,
                                 "incidents": 0, "review_rounds": 5, "stuck_cards": 0})
        p = packet(home, "--prior", prior)
        self.assertEqual(p["prior_window"], [S - 7 * 86400, S])
        self.assertEqual(p["worse"], [
            {"metric": "cost_per_card", "before": 10.0, "after": 100.0},
            {"metric": "incidents", "before": 0, "after": 1},
            {"metric": "landed", "before": 3, "after": 1},
            {"metric": "tokens", "before": 50, "after": 100}])

    def test_landed_up_and_other_metrics_down_are_not_worse(self):
        home, path = make_board()
        card(path, "done", completed=IN)
        card(path, "done", completed=IN)
        prior = self.prior_file({"landed": 1, "incidents": 4, "tokens": 0})
        self.assertEqual(packet(home, "--prior", prior)["worse"], [])

    def test_a_metric_missing_either_side_is_skipped(self):
        home, path = make_board()      # flaky_tests and interventions are missing here
        prior = self.prior_file({"flaky_tests": 0, "interventions": 0, "landed": 0})
        self.assertEqual(packet(home, "--prior", prior)["worse"], [])
        home2, path2 = make_board()
        card(path2, "done", completed=IN)
        self.assertEqual(packet(home2, "--prior", self.prior_file({}))["worse"], [])

    def test_null_cost_per_card_is_skipped(self):
        home, _ = make_board()
        prior = self.prior_file({"cost_per_card": 5.0, "landed": 0})
        self.assertEqual(packet(home, "--prior", prior)["worse"], [])

    def test_bad_prior_is_refused(self):
        home, _ = make_board()
        f = tmp() / "p.json"
        good = {"schema": "audit_packet.v1", "metrics": {}, "window": [1, 2]}
        for bad in ({"schema": "nope"}, {**good, "window": None}, {**good, "window": [1]},
                    {**good, "window": [1, "x"]}, {**good, "window": [1, 2, 3]},
                    {**good, "metrics": []}, [1]):
            f.write_text(json.dumps(bad))
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc, _ = run(home, "--prior", str(f))
            self.assertEqual(rc, 2, bad)
            self.assertIn("--prior", err.getvalue())


class Trigger(unittest.TestCase):
    def rounds(self, path, n, started):
        t = card(path)
        for _ in range(n):
            add_run(path, t, started=started)

    def test_thirty_rounds_since_window_start_trigger_early(self):
        home, path = make_board()
        self.rounds(path, 29, IN)
        self.assertEqual(packet(home)["trigger"], "weekly")
        self.rounds(path, 1, IN)
        self.assertEqual(packet(home)["trigger"], "rounds")

    def test_rounds_are_counted_from_the_prior_window_end(self):
        home, path = make_board()
        self.rounds(path, 20, S + 10)
        self.rounds(path, 10, S + 5 * 86400)
        f = tmp() / "prior.json"
        f.write_text(json.dumps({"schema": "audit_packet.v1", "window": [S - 7 * 86400, S + 86400],
                                 "metrics": {}}))
        self.assertEqual(packet(home, "--prior", str(f))["trigger"], "weekly")
        f.write_text(json.dumps({"schema": "audit_packet.v1", "window": [S - 7 * 86400, S],
                                 "metrics": {}}))
        self.assertEqual(packet(home, "--prior", str(f))["trigger"], "rounds")


class Output(unittest.TestCase):
    def test_deterministic_and_read_only(self):
        home, path = make_board()
        t = card(path, "done", completed=IN)
        add_run(path, t, tok=(5, 5, 5))
        incident(path, "inc_00000001", IN)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        first, second = run(home)[1], run(home)[1]
        self.assertEqual(first, second)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_out_writes_a_file_and_no_stdout(self):
        home, _ = make_board()
        out = tmp() / "packet.json"
        rc, text = run(home, "--out", str(out))
        self.assertEqual((rc, text), (0, ""))
        self.assertEqual(json.loads(out.read_text())["board"], BOARD)
        self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ["packet.json"])
        self.assertEqual(out.read_text(), run(home)[1])

    def test_default_window_is_the_last_seven_days(self):
        home, _ = make_board()
        w = packet(home, window=None)["window"]
        self.assertEqual(w[1] - w[0], 7 * 86400)


if __name__ == "__main__":
    unittest.main()
