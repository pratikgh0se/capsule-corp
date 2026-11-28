"""Evals E2b (t_e4e9c324, row 617): the `fresh_tokens` oracle. Oracle 1 is the real module on a
synthetic spool dir and synthetic transcripts; oracle 2 is the hand numbers written in each test
(A from the task_runs columns, B from the usage lines of the transcripts). Never live data."""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import db, eval_check, eval_tokens, intent
from capsule.tests import tmpclean

SINCE, UNTIL, NOW = 1000, 1000 + 7 * 86400, 1000 + 9 * 86400


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.home, self.board = self.dir / "home", "b"
        (self.home / "boards" / "b").mkdir(parents=True)
        self.spool, self.projects = self.home / "boards" / "b" / "spool", self.dir / "proj"
        (self.projects / "slug").mkdir(parents=True)
        self.conn = db.connect(self.dir / "k.db")
        self.addCleanup(self.conn.close)
        db.create_board(self.dir / "k.db")
        self.n = 0

    def run_row(self, fresh, transcript=(), run_json=True, task="t_1"):
        """A task_run with A = `fresh` (all in tokens_in) and a transcript of
        (message id, in, out, cache write, cache read) lines."""
        self.n += 1
        rid, sid = f"run{self.n}", f"sess-{self.n}"
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id, model, tokens_in)"
            " VALUES (?, 'implementer', 'done', ?, ?, 'm-a', ?)",
            (task, SINCE + self.n, rid, fresh))
        if run_json:
            (self.spool / rid).mkdir(parents=True)
            (self.spool / rid / "run.json").write_text(json.dumps({"session_id": sid}))
        if transcript:
            lines = [{"type": "user", "message": {"content": "NEVER READ"}}] + [
                {"type": "assistant", "message": {"id": m, "model": "m-b", "usage": {
                    "input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": w,
                    "cache_read_input_tokens": r}}} for m, i, o, w, r in transcript]
            (self.projects / "slug" / f"{sid}.jsonl").write_text(
                "\n".join(json.dumps(x) for x in lines))

    def check(self, **kw):
        return eval_check.check(
            self.conn, self.home, self.board, since=SINCE, until=UNTIL, now=NOW, vault="/none",
            metrics=("fresh_tokens",), projects=self.projects, ledger=self.dir / "none.md", **kw)[0]


class Tokens(Case):
    def test_an_agreeing_run_agrees_and_keeps_the_four_fields_apart(self):
        self.run_row(1000, [("m1", 100, 300, 600, 5000)])   # fresh 100+300+600, cache read apart
        got = self.check()
        self.assertEqual((got["a"], got["b"], got["agree"]), (1000, 1000, True))
        self.assertEqual(got["detail"]["usage"], {
            "input_tokens": 100, "output_tokens": 300, "cache_creation_input_tokens": 600,
            "cache_read_input_tokens": 5000})
        self.assertEqual(got["detail"]["per_card"], {"t_1": {"a": 1000, "b": 1000}})
        self.assertEqual(got["detail"]["per_model"], {"a": {"m-a": 1000}, "b": {"m-b": 1000}})

    def test_a_repeated_message_id_counts_once_at_the_per_field_maximum(self):
        self.run_row(500, [("m1", 10, 20, 0, 0), ("m1", 10, 400, 0, 0), ("m2", 0, 70, 0, 0)])
        got = self.check()
        self.assertEqual((got["b"], got["agree"]), (480, True))   # 10+400 and 70, not 10+20+...

    def test_a_run_30_percent_short_is_a_disagreement_and_opens_one_incident(self):
        self.run_row(1000, [("m1", 1000, 0, 0, 0)])
        self.run_row(1000, [("m1", 700, 0, 0, 0)], task="t_2")
        self.run_row(50)    # no run.json: unjoined, 2.4% of A, below the limit
        got = self.check()
        self.assertEqual((got["a"], got["b"], got["agree"]), (2050, 1700, False))
        self.assertEqual(got["detail"]["unjoined"], [{"task_id": "t_1", "run_id": "run3", "a": 50}])
        self.assertEqual(got["detail"]["per_card"]["t_2"], {"a": 1000, "b": 700})
        inc = self.conn.execute("SELECT root_key FROM incidents WHERE role='eval'").fetchall()
        self.assertEqual(len(inc), 1)
        self.assertIn("eval:fresh_tokens", inc[0][0])

    def test_a_small_unjoined_share_does_not_break_agreement(self):
        self.run_row(1000, [("m1", 1000, 0, 0, 0)])
        self.run_row(100)
        got = self.check()    # 9% unjoined: a 1100 in all, but joined A 1000 is what meets B 1000
        self.assertEqual((got["a"], got["b"], got["agree"]), (1100, 1000, True))

    def test_unjoined_tokens_are_not_held_against_the_rel_tolerance(self):
        self.run_row(1000, [("m1", 900, 0, 0, 0)])    # joined A 1000 vs B 900: 11% apart, agrees
        self.run_row(100)
        self.assertTrue(self.check()["agree"])

    def test_unjoined_above_15_percent_disagrees_even_when_the_rest_matches(self):
        self.run_row(850, [("m1", 850, 0, 0, 0)])
        self.run_row(300, run_json=True)    # run.json but no transcript
        got = self.check()
        self.assertEqual((got["agree"], len(got["detail"]["unjoined"])), (False, 1))
        self.assertIn("unjoined", got["note"])

    def test_a_run_with_no_run_id_is_unjoined_and_an_empty_window_agrees(self):
        self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, tokens_in)"
                          " VALUES ('t_1', 'implementer', 'done', ?, 40)", (SINCE + 1,))
        self.assertEqual(self.check()["detail"]["unjoined"][0]["a"], 40)

    def test_the_window_end_is_exclusive_and_the_tolerance_file_has_the_metric(self):
        self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, tokens_in)"
                          " VALUES ('t_1', 'implementer', 'done', ?, 40)", (UNTIL,))
        got = self.check()
        self.assertEqual((got["a"], got["agree"]), (0, True))
        tol = eval_check.load_tolerances()["fresh_tokens"]
        self.assertEqual((tol["rel"], tol["unjoined"]), (0.15, 0.15))

    def test_a_session_id_cannot_escape_the_projects_dir(self):
        self.run_row(10)
        (self.spool / "run1" / "run.json").write_text(json.dumps({"session_id": "../x"}))
        self.assertIsNone(eval_tokens._transcript(self.spool, self.projects, "run1"))
        self.assertIsNone(eval_tokens._transcript(self.spool, self.projects, "../run1"))


class Intent(Case):
    def test_every_tokens_example_is_judged_by_the_real_check(self):
        seen = 0
        for ex in intent.load("eval_check"):
            doc = ex["given"]["document"]
            if "tokens" not in doc:
                continue
            seen += 1
            for run in doc["tokens"]:
                msgs = run["msgs"]
                self.run_row(run["a"], [(m, n, 0, 0, 0) for m, n in msgs or ()],
                             run_json=msgs is not None)
            got = self.check()
            found = {"agree": got["agree"], "incidents": self.conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE role='eval'").fetchone()[0]}
            self.assertEqual(intent._differs(ex["expect"], found, None), [], ex["id"])
            self.conn.execute("DELETE FROM task_runs")
            self.conn.execute("DELETE FROM incidents")
            self.conn.execute("DELETE FROM task_events WHERE kind='eval_check'")
        self.assertEqual(seen, 3)


if __name__ == "__main__":
    unittest.main()
