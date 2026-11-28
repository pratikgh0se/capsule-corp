"""Task 7a (The Lookout): the live panel smoke (spec 10.4). Oracle 1: the status lines for fixture
dashboards run against a fake cursor. Oracle 2: the real committed Lookout JSON, whose every
macro and variable must be substituted before a query reaches the database."""
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "maintenance"))
import lookout_smoke as ls  # noqa: E402
from capsule import legacy_names as _LN  # noqa: E402

PG = {"type": "grafana-postgresql-datasource", "uid": "ops-pg"}


def panel(title, sql, ds=PG):
    return {"title": title, "type": "stat", "datasource": ds, "targets": [{"rawSql": sql}]}


DASH = {
    "uid": "t-home",
    "templating": {"list": [
        {"name": "product", "type": "query", "multi": True,
         "query": "select distinct board from b"},
        {"name": "task_id", "type": "query", "multi": False,
         "query": "select t from c where board in ($product)"},
        {"name": "era", "type": "custom", "query": f"capsule : capsule,{_LN.NAME} : {_LN.NAME}",
         "current": {"value": "capsule"}},
    ]},
    "panels": [
        panel("Factory", "select 1 from v where board in ($product) and era = '$era'"),
        panel("Quiet", "select 1 where $__timeFilter(ts) and t = '$task_id'"),
        panel("Broken", "select nope"),
        panel("Log", "ignored", ds={"type": "loki", "uid": "ops-loki"}),
        {"title": "Notes", "type": "text"},
    ],
}


class FakeDb:
    """Stands in for the database: records every query, answers by its text."""
    def __init__(self):
        self.seen = []

    def __call__(self, sql):
        self.seen.append(sql)
        if "nope" in sql:
            raise RuntimeError("column nope does not exist")
        if "distinct board" in sql:
            return ["a", "b"]
        if "from c" in sql:
            return ["t_1", "t_2"]
        return [] if "ts between" in sql else ["1"]  # "Quiet" is empty, the rest have a row


def smoke(dash, run):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "d.json").write_text(json.dumps(dash))
        with redirect_stdout(io.StringIO()) as out:
            rc = ls.main([], run=run, dash_dir=tmp)
    return rc, out.getvalue()


class Smoke(unittest.TestCase):
    def test_statuses_and_substitution(self):
        db = FakeDb()
        rc, out = smoke(DASH, db)
        self.assertEqual(rc, 1)
        for line in ("PASS  t-home panel 'Factory'", "EMPTY t-home panel 'Quiet'",
                     "FAIL  t-home panel 'Broken': column nope", "SKIP  t-home panel 'Log'",
                     "SKIP  t-home panel 'Notes'"):
            self.assertIn(line, out)
        self.assertIn("select 1 from v where board in ('a','b') and era = 'capsule'", db.seen)
        self.assertIn("select 1 where ts between now() - interval '24 hours' and now()"
                      " and t = 't_1'", db.seen)

    def test_empty_must_have_panel_fails(self):
        dash = dict(DASH, panels=[panel("Budget windows", "select 1 where false")])
        rc, out = smoke(dash, lambda sql: [])
        self.assertEqual(rc, 1)
        self.assertIn("empty, must have data", out)

    def test_failing_variable_query_is_reported(self):
        def run(sql):
            raise RuntimeError("boom")
        rc, out = smoke(DASH, run)
        self.assertIn("FAIL  t-home variable 'product': boom", out)
        self.assertIn("FAIL  t-home variable 'task_id': boom", out)

    def test_quote_in_value_is_escaped(self):
        v = {"p": (["o'x"], False, False)}
        self.assertEqual(ls.substitute("a = '$p'", v), "a = 'o''x'")

    def test_psql_failure_with_no_output_is_a_runtime_error(self):
        import events
        from unittest import mock
        bad = mock.Mock(returncode=3, stdout="", stderr="")
        with mock.patch.object(events, "docker_run", return_value=bad):
            with self.assertRaisesRegex(RuntimeError, "psql exit 3"):
                ls._psql("select 1")

    def test_no_json_exits_2(self):
        self.assertEqual(ls.main([], run=lambda s: [], dash_dir="/nonexistent-dir"), 2)

    def test_committed_json_leaves_no_macro_or_variable_behind(self):
        seen = []
        with redirect_stdout(io.StringIO()):
            ls.main([], run=lambda sql: seen.append(sql) or ["x"])
        self.assertGreater(len(seen), 50)
        for sql in seen:
            self.assertIsNone(re.search(r"\$[_A-Za-z{]", sql), sql[:120])


if __name__ == "__main__":
    unittest.main()
