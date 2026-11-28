"""One state root (t_c8b3d583 U1): every maintenance python resolves its state
dir through events.state_root(), never from __file__ or the vault symlink.

Oracle 1 is behavioural (fresh subprocess per module, real import, real
constants); oracle 2 is an AST scan of maintenance/*.py for the old idioms.
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

MAINT = Path(__file__).resolve().parents[1]

# module -> [(attribute, relative path under the state root)]
CONSTANTS = {
    "board_fence": [("SKIP_STAMP", "capsule-owned-skip.json")],
    "board_bridge": [("NOTIFY_SEEN", "notify-seen.json"),
                     ("STATE_DIR", "")],
    "ai_usage": [("STATE", "samplers.json")],
    "brief": [("STATE", "")],
    "budgets": [("STATE", "")],
    "usage_guard": [("STATE", "")],
    "messages": [("STATE", "")],
    "retrieval_eval_job": [("STATE_DIR", "")],
    "obs_probe": [("DEFAULT_STATE_DIR", "")],
    "absence": [("_state_dir()", "")],
    "heal_table": [("DEFAULT_HEAL", "heal.json")],
}

PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import importlib
mod = importlib.import_module(sys.argv[2])
print(json.dumps({a: str(eval("mod." + a)) for a in json.loads(sys.argv[3])}))
"""


def _resolve(maint_dir, module, env):
    attrs = [a for a, _ in CONSTANTS[module]]
    base = {"PATH": os.environ["PATH"]}
    base.update(env)
    out = subprocess.run([sys.executable, "-c", PROBE, str(maint_dir), module, json.dumps(attrs)],
                         env=base, capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise AssertionError(f"{module}: {out.stderr[-600:]}")
    return json.loads(out.stdout)


def _expect(root, module):
    return {a: str(Path(root) / rel) if rel else str(root) for a, rel in CONSTANTS[module]}


class ResolvesThroughOneRoot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="state-root-"))
        cls.copy = cls.tmp / "release-copy"
        cls.copy.mkdir()
        for f in (p for p in MAINT.glob("*") if p.is_file()):
            shutil.copy(f, cls.copy / f.name)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_env_default_is_the_local_state_root(self):
        home = self.tmp / "home"
        want_root = home / ".local" / "state" / "capsule-corp" / "maint"
        for module in CONSTANTS:
            with self.subTest(module=module):
                self.assertEqual(_resolve(MAINT, module, {"HOME": str(home)}),
                                 _expect(want_root, module))

    def test_override_moves_every_constant(self):
        x = self.tmp / "x"
        for module in CONSTANTS:
            with self.subTest(module=module):
                got = _resolve(MAINT, module, {"HOME": str(self.tmp / "h"),
                                               "AGENT_OPS_STATE_DIR": str(x)})
                self.assertEqual(got, _expect(x, module))

    def test_override_beats_no_send(self):
        x = self.tmp / "x2"
        for module in CONSTANTS:
            with self.subTest(module=module):
                got = _resolve(MAINT, module, {"HOME": str(self.tmp / "h"),
                                               "AGENT_OPS_NO_SEND": "1",
                                               "AGENT_OPS_STATE_DIR": str(x)})
                self.assertEqual(got, _expect(x, module))

    def test_a_copy_resolves_exactly_like_the_original(self):
        home = self.tmp / "home2"
        for module in CONSTANTS:
            with self.subTest(module=module):
                env = {"HOME": str(home)}
                self.assertEqual(_resolve(self.copy, module, env), _resolve(MAINT, module, env))
                env["AGENT_OPS_STATE_DIR"] = str(self.tmp / "y")
                self.assertEqual(_resolve(self.copy, module, env), _expect(self.tmp / "y", module))

    def test_no_send_without_override_never_yields_the_real_root(self):
        home = self.tmp / "home3"
        env = {"HOME": str(home), "AGENT_OPS_NO_SEND": "1"}
        modules = ("ai_usage", "brief", "messages", "heal_table", "board_bridge", "board_fence")
        for module in modules:
            with self.subTest(module=module):
                got = _resolve(MAINT, module, env)
                for v in got.values():
                    self.assertNotIn(".local/state/capsule-corp", v)
                    self.assertNotIn(str(MAINT / "state"), v)
                    self.assertIn("capsule-corp-state-no-send", v)


class NoOldIdiomRemains(unittest.TestCase):
    OLD = ("__file__", "MAINT", ".config")

    def _names(self, node):
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                yield n.id
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                yield n.value

    def test_no_file_relative_or_vault_symlink_state_path(self):
        hits = []
        for f in sorted(MAINT.glob("*.py")):
            tree = ast.parse(f.read_text())
            for n in ast.walk(tree):
                if (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)
                        and isinstance(n.right, ast.Constant) and n.right.value == "state"
                        and any(x in self.OLD for x in self._names(n.left))):
                    hits.append(f"{f.name}:{n.lineno}")
                if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and ".config/capsule-corp/maintenance/state" in n.value):
                    hits.append(f"{f.name}:{n.lineno}")
        self.assertEqual(hits, [])


class HomeUnavailable(unittest.TestCase):
    """state_root() never raises when the uid has no home (worker container)."""

    def setUp(self):
        sys.path.insert(0, str(MAINT))
        self.addCleanup(sys.path.remove, str(MAINT))
        import events
        self.events = events
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGENT_OPS_STATE_DIR", "AGENT_OPS_NO_SEND")}
        patcher = unittest.mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_falls_back_to_tempdir_when_home_raises(self):
        with unittest.mock.patch.object(Path, "home", side_effect=RuntimeError("no home")):
            root = self.events.state_root()
        self.assertEqual(root, Path(tempfile.gettempdir()) / ".local" / "state"
                         / "capsule-corp" / "maint")

    def test_normal_path_unchanged(self):
        with unittest.mock.patch.object(Path, "home", return_value=Path("/h")):
            root = self.events.state_root()
        self.assertEqual(root, Path("/h/.local/state/capsule-corp/maint"))


if __name__ == "__main__":
    unittest.main()
