"""board_query.py (row 633): the agent-review job's one door into a board's kanban.db.
It replaced `Bash(sqlite3 -readonly *)`, which could run `.system`, `writefile()` and ATTACH."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule import intent  # noqa: E402

MAINT = Path(__file__).resolve().parents[1]
SCRIPT = MAINT / "board_query.py"


class BoardQuery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        db = self.home / "boards" / "alpha" / "kanban.db"
        db.parent.mkdir(parents=True)
        with sqlite3.connect(db) as conn:
            conn.execute("create table task_runs (id integer, outcome text)")
            conn.executemany("insert into task_runs values (?, ?)",
                             [(i, "done" if i % 2 else None) for i in range(1, 601)])
        self.db = db
        self.leak = Path(self.tmp.name) / "leak.txt"

    def run_q(self, board, sql):
        env = dict(os.environ, CAPSULE_HOME=str(self.home), AGENT_OPS_NO_SEND="1")
        return subprocess.run([sys.executable, str(SCRIPT), board, sql], env=env,
                              capture_output=True, text=True, timeout=30)

    def assertRefused(self, board, sql):
        r = self.run_q(board, sql)
        self.assertEqual(r.returncode, 2, (sql, r.stdout, r.stderr))
        self.assertFalse(self.leak.exists(), sql)
        return r

    def test_a_select_prints_a_tsv_header_and_rows(self):
        r = self.run_q("alpha", "select id, outcome from task_runs where id <= 2 order by id")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["id\toutcome", "1\tdone", "2\t"])

    def test_plain_functions_and_aggregates_work(self):
        r = self.run_q("alpha", "select count(*), max(id), upper(min(outcome)) from task_runs")
        self.assertEqual(r.stdout.splitlines()[1], "600\t600\tDONE")

    def test_the_row_cap_truncates(self):
        r = self.run_q("alpha", "select id from task_runs")
        self.assertEqual(r.returncode, 0)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 1 + 500 + 1)
        self.assertEqual(lines[-1], "... truncated at 500 rows")

    def test_attach_is_refused(self):
        self.assertRefused("alpha", f"attach database '{self.leak}' as x")
        self.assertRefused("alpha", f"select 1; attach database '{self.leak}' as x")

    def test_pragma_is_refused(self):
        self.assertRefused("alpha", "pragma table_info(task_runs)")
        self.assertRefused("alpha", "select * from pragma_table_info('task_runs')")

    def test_writes_are_refused_and_change_nothing(self):
        self.assertRefused("alpha", "insert into task_runs values (999, 'x')")
        self.assertRefused("alpha", "delete from task_runs")
        self.assertRefused("alpha", "drop table task_runs")
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("select count(*) from task_runs").fetchone()[0], 600)

    def test_a_second_statement_is_refused(self):
        self.assertRefused("alpha", "select 1; select 2")
        self.assertRefused("alpha", "select 1; delete from task_runs")

    def test_file_functions_are_refused(self):
        self.assertRefused("alpha", f"select writefile('{self.leak}', 'x')")
        self.assertRefused("alpha", "select readfile('/etc/hosts')")
        self.assertRefused("alpha", "select load_extension('x')")

    def test_a_function_off_the_allow_list_is_refused_by_the_authorizer(self):
        # random() and sqlite_version() exist, so only the authorizer can refuse them
        self.assertRefused("alpha", "select random()")
        self.assertRefused("alpha", "select sqlite_version()")

    def test_a_board_name_only_never_a_path(self):
        for name in (str(self.db), str(self.db.parent), "../alpha", "alpha/..", "a/b", "", "new"):
            self.assertRefused(name, "select 1")

    def test_an_over_long_statement_is_refused(self):
        r = self.assertRefused("alpha", "select 1 where 1 = 1" + " and 1 = 1" * 400)
        self.assertIn("longer than", r.stderr)

    def test_a_runaway_query_is_aborted_by_the_time_budget(self):
        started = time.monotonic()
        r = self.assertRefused(
            "alpha", "select count(*) from task_runs a, task_runs b, task_runs c, task_runs d")
        self.assertIn("budget", r.stderr)
        self.assertLess(time.monotonic() - started, 15)

    def observe(self, document):
        """What the intent rows describe: run the script, report what happened."""
        if "sql" in document:
            r = self.run_q(document["board"], document["sql"])
            lines = r.stdout.splitlines()
            return {"exit": r.returncode, "header": lines[0] if lines else "",
                    "first_row": lines[1] if len(lines) > 1 else "", "row_count": len(lines) - 1}
        codes = [self.run_q(document["board"], s.replace("__LEAK__", str(self.leak))).returncode
                 for s in document["statements"]]
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute("select count(*) from task_runs").fetchone()[0]
        return {"exit_codes": ",".join(map(str, codes)), "files_created": int(self.leak.exists()),
                "rows_unchanged": rows == 600}

    def test_the_shipped_intent_rows_are_all_replayed(self):
        """Z1: contracts/intent/board_query.jsonl, each row observed and matched."""
        rows = intent.load("board_query")
        self.assertEqual({r["id"] for r in rows},
                         {"board-query-select-prints-tsv", "board-query-refuses-escapes"})
        for row in rows:
            with self.subTest(row["id"]):
                got = self.observe(row["given"]["document"])
                self.assertTrue(intent.matches(row["expect"], got), (row["id"], got))

    def test_an_unknown_board_and_the_wrong_arity_fail_closed(self):
        self.assertRefused("beta", "select 1")
        env = dict(os.environ, CAPSULE_HOME=str(self.home), AGENT_OPS_NO_SEND="1")
        r = subprocess.run([sys.executable, str(SCRIPT), "alpha"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
