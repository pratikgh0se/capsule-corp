"""M137 parts that stand alone (the card flow is in test_image_requests): the capsule.toml line
edit and its tomllib oracle, the host ledger under two writers, the reserved assignee, the
protected-path glob and `blocks.promote_one`."""
import shutil
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from capsule import blocks, config, db, image_ledger, image_toml, workspace

NOW = 1_800_000_000
TOML = ('# the project image\n[image]\nstacks = ["python"]\nsmoke = ["python -V"]  # first\n\n'
        '[other]\nx = 1\n')


class Parts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_10_worker_diff_touching_capsule_toml_is_protected_once_the_glob_lands(self):
        landed = "capsule.toml" in workspace.PROTECTED
        self.assertEqual(workspace.touches_protected(["capsule.toml"]),
                         ["capsule.toml"] if landed else [])
        with mock.patch.object(workspace, "PROTECTED", (*workspace.PROTECTED, "capsule.toml")):
            self.assertEqual(workspace.touches_protected(["capsule.toml", "x.py"]),
                             ["capsule.toml"])

    def test_two_concurrent_ledger_writers_never_tear_a_line(self):
        def writer(n):
            for i in range(60):
                image_ledger.append(self.tmp, "filed", NOW, board=f"b{n}", task=f"t{i}",
                                    tool="x" * 200)
        threads = [threading.Thread(target=writer, args=(n,)) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(image_ledger.rows(self.tmp, "filed")), 120)
        self.assertEqual(len(image_ledger.path(self.tmp).read_text().splitlines()), 120)

    def test_ledger_skips_torn_lines_and_filters_by_day_and_field(self):
        image_ledger.append(self.tmp, "try", NOW, task="a", minutes=3)
        image_ledger.append(self.tmp, "try", NOW - 3 * 86400, task="a", minutes=9)
        with open(image_ledger.path(self.tmp), "a") as fh:
            fh.write('{"kind": "try", "task"\n')
        self.assertEqual(len(image_ledger.rows(self.tmp, "try")), 2)
        self.assertEqual([r["minutes"] for r in image_ledger.rows(self.tmp, "try", today=NOW)], [3])
        self.assertEqual(image_ledger.rows(self.tmp, "try", task="b"), [])

    def test_toml_edit_changes_only_apt_and_smoke(self):
        new, why = image_toml.add_item(TOML, "apt", "ripgrep")
        new, why2 = image_toml.add_item(new, "smoke", "command -v rg")
        self.assertEqual((why, why2), (None, None))
        old, now = tomllib.loads(TOML), tomllib.loads(new)
        old["image"].update(apt=["ripgrep"], smoke=["python -V", "command -v rg"])
        self.assertEqual(now, old)
        self.assertIn("# the project image", new)
        self.assertIn('smoke = ["python -V", "command -v rg"]  # first', new)
        self.assertEqual(image_toml.add_item(new, "apt", "ripgrep")[0], new)

    def test_toml_edit_refuses_what_it_cannot_do_by_line(self):
        cases = (('[image]\nstacks = ["python"]\napt = [\n  "a",\n]\n', "multi-line"),
                 ('[image]\nsmoke = []\n', "stacks"), ('[x]\ny = 1\n', "stacks"),
                 ("[image\n", "parse"),
                 ('[image]\nstacks = ["python"]\n[ other ]\n', None),
                 ('image = {stacks = ["python"]}\n', "plain [image] header"),
                 ('[ image ]\nstacks = ["python"]\n', "plain [image] header"))
        for text, word in cases:
            if word:
                self.assertIn(word, image_toml.add_item(text, "apt", "ripgrep")[1])

    def test_assignee_image_builder_is_refused_in_config(self):
        with self.assertRaises(ValueError):
            config.validate_assignees({"image-builder": "implementer"})

    def test_promote_one_waits_for_the_last_open_parent(self):
        db.create_board(self.tmp / "k.db")
        conn = db.connect(self.tmp / "k.db")
        self.addCleanup(conn.close)
        a = db.new_task(conn, title="a", status="ready", now=NOW)
        b = db.new_task(conn, title="b", status="ready", now=NOW)
        c = db.new_task(conn, title="c", status="todo", parents=(a, b), now=NOW)
        db.transition(conn, a, "done", frm=("ready",))
        self.assertFalse(blocks.promote_one(conn, c))
        db.transition(conn, b, "done", frm=("ready",))
        self.assertTrue(blocks.promote_one(conn, c))


if __name__ == "__main__":
    unittest.main()
