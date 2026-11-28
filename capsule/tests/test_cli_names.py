"""`capsule names backfill`: numbering, open-vs-done naming, draft marker, dry-run; model faked."""
import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from capsule import cli_names, db, paths


def cli_names_models():
    from capsule import models
    return models


def fake_draft(items):
    return {tid: "Fix the thing now" for tid, _ in items}


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.path = paths.board_db("b1", self.home)
        db.create_board(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)

    def _old(self, tid, created, status="ready", number=None, name=None):
        self.conn.execute(
            "INSERT INTO tasks (id, title, body, status, created_at, created_by, number,"
            " short_name) VALUES (?,?,?,?,?,?,?,?)",
            (tid, f"title {tid}", "", status, created, "t", number, name))

    def _col(self, col):
        return {r["id"]: r[col] for r in self.conn.execute(f"SELECT id, {col} FROM tasks")}

    def test_numbers_fill_gaps_in_created_order_and_keep_existing(self):
        self._old("t_c", 5)
        self._old("t_b", 5)
        self._old("t_a", 1)
        self._old("t_x", 9, number=2)
        cli_names.backfill(self.conn, draft=fake_draft)
        self.assertEqual(self._col("number"), {"t_a": 1, "t_b": 3, "t_c": 4, "t_x": 2})

    def test_second_run_changes_nothing(self):
        self._old("t_a", 1)
        self._old("t_b", 2, status="done")
        cli_names.backfill(self.conn, draft=fake_draft)
        self.assertEqual(cli_names.backfill(self.conn, draft=fake_draft), [])

    def test_only_open_unnamed_tasks_get_names_marked_draft(self):
        for tid, st in (("t_r", "ready"), ("t_d", "done"), ("t_z", "archived")):
            self._old(tid, 1, status=st)
        self._old("t_h", 1, name="Hand name")
        self.conn.execute("UPDATE tasks SET name_source='human' WHERE id='t_h'")
        asked = []

        def draft(items):
            asked.extend(tid for tid, _ in items)
            return fake_draft(items)
        cli_names.backfill(self.conn, draft=draft)
        self.assertEqual(sorted(asked), ["t_r"])
        self.assertEqual(self._col("short_name"),
                         {"t_r": "Fix the thing now", "t_d": None, "t_z": None, "t_h": "Hand name"})
        self.assertEqual(self._col("name_source"),
                         {"t_r": "draft", "t_d": None, "t_z": None, "t_h": "human"})

    def test_tasks_added_during_drafting_get_no_duplicate_number(self):
        self._old("t_a", 1)

        def draft(items):
            self._old("t_late", 0)      # lands while the model is thinking
            self.conn.execute("DELETE FROM tasks WHERE id='t_a'")
            self._old("t_b", 2)
            return fake_draft(items)
        cli_names.backfill(self.conn, draft=draft)
        nums = self._col("number")
        self.assertEqual(sorted(nums), ["t_b", "t_late"])
        self.assertEqual(sorted(nums.values()), [1, 2])

    def test_title_cannot_forge_a_line_for_another_task(self):
        seen = []
        items = [("t_a", "x\nt_other\tX"), ("t_b", "y")]
        route = argparse.Namespace(report={"names": []}, error_class=None, tokens={"in": 3})
        adapter = argparse.Namespace(run=lambda role, bundle, work, limits: (
            seen.append((bundle["prompt"], limits)) or route))
        real = cli_names_models().adapter_for
        cli_names_models().adapter_for = lambda r: adapter
        self.addCleanup(setattr, cli_names_models(), "adapter_for", real)
        cli_names.TOKENS.clear()
        cli_names._draft_with_haiku(items)
        cli_names._draft_with_haiku(items)
        self.assertEqual(cli_names.TOKENS, {"in": 6})
        prompt, limits = seen[0]
        self.assertEqual(prompt.split("\n"), ["t_a\tx t_other X", "t_b\ty"])
        self.assertEqual(limits["tools"], "")

    def test_bad_word_counts_and_unasked_ids_are_dropped(self):
        self._old("t_a", 1)
        reply = {"t_a": "Too short", "t_q": "Four words in here"}
        out = cli_names._plan_names([("t_a", "x")], lambda items: reply)
        self.assertEqual(out, {})

    def test_batches_titles_into_few_calls(self):
        calls = []
        rows = [(f"t_{i}", "x") for i in range(cli_names.BATCH + 1)]
        cli_names._plan_names(rows, lambda items: calls.append(len(items)) or {})
        self.assertEqual(calls, [cli_names.BATCH, 1])

    def test_dry_run_prints_lines_and_writes_nothing(self):
        self._old("t_a", 1)
        self.conn.commit()
        args = argparse.Namespace(board="b1", dry_run=True, capsule_home=str(self.home))
        real, cli_names._draft_with_haiku = cli_names._draft_with_haiku, fake_draft
        self.addCleanup(setattr, cli_names, "_draft_with_haiku", real)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_names.handle(args, None)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "1 Fix the thing now <- title t_a")
        self.assertEqual(self._col("number"), {"t_a": None})
        self.assertEqual(self._col("short_name"), {"t_a": None})


if __name__ == "__main__":
    unittest.main()
