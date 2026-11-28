"""EVAL-03: golden-set runner and its grader, with a fake adapter (no model calls)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from capsule import golden


class FakeAdapter:
    """Returns the report stored under the case dir name; records each call."""

    def __init__(self, reports):
        self.reports = reports
        self.calls = []

    def run(self, role, bundle, workdir, env):
        self.calls.append((role, bundle, Path(workdir).name))
        return SimpleNamespace(report=self.reports[Path(workdir).name])


def _case(root, name, expect, bundle="b"):
    d = Path(root) / name
    d.mkdir()
    (d / "case.json").write_text(json.dumps({"bundle": bundle, "expect": expect}))


class GradeTests(unittest.TestCase):
    def test_subset_match_passes(self):
        self.assertTrue(golden._grade({"a": 1}, {"a": 1, "b": 2}))

    def test_wrong_value_or_missing_key_fails(self):
        self.assertFalse(golden._grade({"a": 1}, {"a": 2}))
        self.assertFalse(golden._grade({"a": 1}, {"b": 1}))

    def test_empty_expect_grades_false(self):
        self.assertFalse(golden._grade({}, {"a": 1}))

    def test_non_dict_report_grades_false(self):
        for report in (None, "x", ["a"], 3):
            self.assertFalse(golden._grade({"a": 1}, report))


class RunTests(unittest.TestCase):
    def _run(self, setup, reports):
        with tempfile.TemporaryDirectory() as tmp:
            setup(tmp)
            adapter = FakeAdapter(reports)
            return golden.run("role-x", tmp, adapter), adapter

    def test_all_pass(self):
        def setup(t):
            _case(t, "c1", {"v": "ok"})
            _case(t, "c2", {"v": "ok"})
        out, adapter = self._run(setup, {"c1": {"v": "ok"}, "c2": {"v": "ok", "x": 1}})
        self.assertEqual(out, {"passed": 2, "failed": 0, "cases": [
            {"case": "c1", "passed": True}, {"case": "c2", "passed": True}]})
        self.assertEqual([c[:2] for c in adapter.calls], [("role-x", "b")] * 2)

    def test_one_fails(self):
        def setup(t):
            _case(t, "c1", {"v": "ok"})
            _case(t, "c2", {"v": "ok"})
        out, _ = self._run(setup, {"c1": {"v": "ok"}, "c2": {"v": "bad"}})
        self.assertEqual((out["passed"], out["failed"]), (1, 1))
        self.assertEqual([c["passed"] for c in out["cases"]], [True, False])

    def test_empty_expect_case_counts_as_failed(self):
        out, _ = self._run(lambda t: _case(t, "c1", {}), {"c1": {"v": 1}})
        self.assertEqual((out["passed"], out["failed"]), (0, 1))

    def test_non_dict_report_case_counts_as_failed(self):
        out, _ = self._run(lambda t: _case(t, "c1", {"v": 1}), {"c1": "oops"})
        self.assertEqual((out["passed"], out["failed"]), (0, 1))

    def test_dir_without_case_json_is_skipped(self):
        def setup(t):
            _case(t, "c1", {"v": 1})
            (Path(t) / "empty").mkdir()
            (Path(t) / "stray.txt").write_text("x")
        out, adapter = self._run(setup, {"c1": {"v": 1}})
        self.assertEqual([c["case"] for c in out["cases"]], ["c1"])
        self.assertEqual(len(adapter.calls), 1)


if __name__ == "__main__":
    unittest.main()
