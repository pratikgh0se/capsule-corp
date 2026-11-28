"""A file-set file with no size room reaches the implementer brief (inc_9c7b6734)."""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import worker


def _repo(root, files, baseline=None):
    for rel, lines in files.items():
        path = Path(root, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n" * lines)
    if baseline is not None:
        Path(root, "tests").mkdir(exist_ok=True)
        Path(root, "tests", "size_baseline.json").write_text(json.dumps(baseline))


class SizeRoom(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_a_file_set_file_at_its_frozen_size_reaches_the_brief_with_both_routes(self):
        _repo(self.root, {"capsule/big.py": 3}, {"capsule/big.py::file_length": 3})
        task = {"id": "t_1", "title": "t", "body": "File set:\n- capsule/big.py\n"}
        prompt = worker._bundle(None, "implementer", task, {"resumed_from": None}, {},
                                workspace_dir=self.root)["prompt"]
        self.assertIn("## Files with no size room", prompt)
        self.assertIn("capsule/big.py", prompt)
        self.assertIn("3 lines", prompt)
        self.assertIn("same-file offset", prompt)
        self.assertIn("new module", prompt)

    def test_a_file_below_its_ceiling_gets_no_line_and_a_card_with_no_full_file_no_section(self):
        _repo(self.root, {"capsule/a.py": 2}, {"capsule/a.py::file_length": 5})
        self.assertEqual(worker._full_files(["capsule/a.py", "capsule/gone.py"], self.root), [])
        task = {"id": "t_1", "title": "t", "body": "File set:\n- capsule/a.py\n"}
        prompt = worker._bundle(None, "implementer", task, {"resumed_from": None}, {},
                                workspace_dir=self.root)["prompt"]
        self.assertNotIn("Files with no size room", prompt)

    def test_a_file_with_no_baseline_entry_is_full_at_800_lines(self):
        _repo(self.root, {"capsule/a.py": 800, "capsule/b.py": 799}, {})
        lines = worker._full_files(["capsule/a.py", "capsule/b.py"], self.root)
        self.assertEqual(len(lines), 1)
        self.assertIn("capsule/a.py", lines[0])
        self.assertIn("800", lines[0])

    def test_a_missing_or_unreadable_baseline_or_file_gives_no_line_and_no_error(self):
        _repo(self.root, {"capsule/a.py": 800})
        self.assertEqual(worker._full_files(["capsule/a.py"], self.root), [])
        Path(self.root, "tests").mkdir()
        Path(self.root, "tests", "size_baseline.json").write_text("{not json")
        self.assertEqual(worker._full_files(["capsule/a.py"], self.root), [])
        Path(self.root, "capsule", "bin.py").write_bytes(b"\xff\xfe\n" * 900)
        self.assertEqual(worker._full_files(["capsule/bin.py"], self.root), [])
        self.assertEqual(worker._full_files(["capsule/a.py"], None), [])

    def test_other_roles_do_not_change(self):
        _repo(self.root, {"capsule/big.py": 3}, {"capsule/big.py::file_length": 3})
        task = {"id": "t_1", "title": "t", "body": "File set:\n- capsule/big.py\n"}
        prompt = worker._bundle(None, "reviewer", task, {}, {"diff": "d"},
                                workspace_dir=self.root)["prompt"]
        self.assertNotIn("Files with no size room", prompt)

    def test_a_non_integer_baseline_value_gives_no_line_and_no_error(self):
        _repo(self.root, {"capsule/a.py": 9}, {"capsule/a.py::file_length": "5"})
        self.assertEqual(worker._full_files(["capsule/a.py"], self.root), [])
