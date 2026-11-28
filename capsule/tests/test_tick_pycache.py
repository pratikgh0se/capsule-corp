"""A bytecode cache under a protected folder is restored, never a row-395 review failure.

A worker's own test run writes gitignored caches such as
`capsule/blind/harness/__pycache__/api.cpython-311.pyc`; `workspace.changed` lists them on
purpose, so `Tick._verdict` used to fail the round. Only `<dir>/__pycache__/<name>.pyc` is
exempt: a `.pyc` outside `__pycache__` shadows its module and a `.py` inside it is code."""
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import tick
from capsule.tests.test_tick import IMPL_DONE, TickCase

CACHE = "capsule/blind/harness/__pycache__/api.cpython-311.pyc"


class IsBytecodeCache(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws, True)

    def test_only_a_pyc_directly_inside_pycache_is_a_cache(self):
        for path, want in ((CACHE, True), ("tests/__pycache__/x.pyc", True),
                           ("capsule/runners/sandbox.pyc", False), ("x.pyc", False),
                           ("capsule/blind/harness/__pycache__/evil.py", False),
                           ("__pycache__/a/x.pyc", False), ("__pycache__", False)):
            self.assertEqual(tick._is_bytecode_cache(self.ws, path), want, path)

    def test_a_cache_named_file_that_survives_restore_is_not_exempt(self):
        kept = Path(self.ws, CACHE)
        kept.parent.mkdir(parents=True)
        kept.write_text("x\n")
        self.assertFalse(tick._is_bytecode_cache(self.ws, CACHE))
        kept.unlink()
        self.assertTrue(tick._is_bytecode_cache(self.ws, CACHE))


class PycacheReview(TickCase):
    def plant_and_review(self, names):
        """Plant `names` in the implementer's clone, run review, return (comments, paths)."""
        self.script["implementer"] = [[IMPL_DONE]]
        planted = []

        def plant(job, sp, run):
            for n in names:
                p = Path(job["workspace"], n)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x\n")
                planted.append(p)
        self.hooks["implementer"] = plant
        self.tick.once()
        self.tick.once()
        self.runner.up = False
        self.tick.once()
        comments = [r[0] for r in self.conn.execute("SELECT body FROM task_comments")]
        return comments, planted

    def test_a_bytecode_cache_under_a_protected_folder_is_removed_not_failed(self):
        comments, planted = self.plant_and_review([CACHE])
        self.assertFalse(any("protected path changed" in c for c in comments), comments)
        self.assertFalse(any(p.exists() for p in planted))

    def test_a_cache_beside_a_real_protected_edit_names_only_the_edit(self):
        comments, planted = self.plant_and_review([CACHE, "contracts/x.md"])
        self.assertTrue(any("(row 395): contracts/x.md" in c for c in comments), comments)
        self.assertFalse(any("__pycache__" in c for c in comments), comments)
        self.assertFalse(any(p.exists() for p in planted))

    def test_a_pyc_outside_pycache_still_fails(self):
        comments, planted = self.plant_and_review(["capsule/runners/sandbox.pyc"])
        self.assertTrue(any("(row 395): capsule/runners/sandbox.pyc" in c
                            for c in comments), comments)
        self.assertFalse(any(p.exists() for p in planted))

    def test_a_py_inside_pycache_still_fails(self):
        # capsule/runners/__pycache__/ is not protected (only sandbox.*, docker.*), so use harness/.
        evil = "capsule/blind/harness/__pycache__/evil.py"
        comments, planted = self.plant_and_review([evil])
        self.assertTrue(any(f"(row 395): {evil}" in c for c in comments), comments)
        self.assertFalse(any(p.exists() for p in planted))
