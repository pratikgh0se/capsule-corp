import ast
import tempfile
import unittest
from pathlib import Path

from capsule import blocks, db


def _is_kind(node, consts):
    """True when `node` is a non-None literal, or a module constant holding one."""
    if isinstance(node, ast.Name):
        return consts.get(node.id) is not None
    return isinstance(node, ast.Constant) and node.value is not None


def _fn_hold(fn, consts):
    """(sets a block_kind through transition, event kinds written) for one function."""
    calls = [(c, c.func.id if isinstance(c.func, ast.Name) else c.func.attr)
             for c in ast.walk(fn) if isinstance(c, ast.Call)
             and isinstance(c.func, (ast.Name, ast.Attribute))]
    held = any(k.arg == "block_kind" and _is_kind(k.value, consts)
               for c, n in calls if n == "transition" for k in c.keywords)
    kinds = {c.args[2].value for c, n in calls if n == "event" and len(c.args) > 2
             and isinstance(c.args[2], ast.Constant)}
    return held, kinds


def _file_writers(path):
    tree = ast.parse(path.read_text())
    consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
              and isinstance(n.value, ast.Constant) for t in n.targets if isinstance(t, ast.Name)}
    fns = [f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)]
    pairs = [(f"{f.name}:{f.lineno}", *_fn_hold(f, consts)) for f in fns]
    return {name: kinds for name, held, kinds in pairs if held}


class HoldEvents(unittest.TestCase):
    """One list of hold and lift event kinds (inc_ac9dafef); a new hold writer must join it."""

    def test_every_block_kind_writer_writes_a_listed_hold_event(self):
        found = {p.name: _file_writers(p) for p in Path(blocks.__file__).parent.glob("*.py")}
        for name in ("claims.py", "runaway.py", "contradiction.py", "restart.py", "blocks.py"):
            self.assertTrue(found.get(name), f"scan found no hold writer in {name}")
        for name, fns in found.items():
            for fn, kinds in fns.items():
                self.assertTrue(kinds & set(blocks.HOLD_EVENTS),
                                f"{name}:{fn} sets block_kind but writes no event in HOLD_EVENTS")
        self.assertIn("failure_limit", blocks.HOLD_EVENTS)
        self.assertIn("blocked", blocks.HOLD_EVENTS)
        self.assertEqual(blocks.LIFT_EVENTS, ("unblocked", "failure_reset"))
        self.assertFalse(set(blocks.LIFT_EVENTS) & set(blocks.HOLD_EVENTS))

    def test_every_event_kind_block_writes_is_a_listed_hold_event(self):
        """block() builds its event kind at run time, which the scan cannot see (inc_9a36be75)."""
        path = Path(tempfile.mkdtemp()) / "kanban.db"
        db.create_board(path)
        conn = db.connect(path)
        self.addCleanup(conn.close)
        tid = db.new_task(conn, title="t")
        written = []
        for kind, route in (("dependency", "todo"), ("needs_input", "blocked"),
                            ("needs_input", "triage")):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
            conn.commit()
            self.assertEqual(blocks.block(conn, tid, kind=kind, reason="r"), route)
            written.append(conn.execute("SELECT kind FROM task_events WHERE task_id=? "
                                        "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])
        self.assertEqual(written, ["dependency_wait", "blocked", "block_loop_detected"])
        unlisted = [k for k in written if k not in blocks.HOLD_EVENTS]
        self.assertEqual(unlisted, [], f"block() writes kinds missing from HOLD_EVENTS: {unlisted}")
        self.assertEqual(len(set(blocks.HOLD_EVENTS)), len(blocks.HOLD_EVENTS))
