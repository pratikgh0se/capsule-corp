"""`db.open_parent`, the one 'parent not done/archived' query (t_0f166241) that
`blocks`, `lease.claim` and `unblocked.dependency_blocked` all share. Z2: oracle 1 is a
hand-built board where the answer is known by construction; oracle 2 drives the helper
through a real caller (`lease.claim`), which shares no code with oracle 1's assertions."""
import re
import tempfile
import unittest
from pathlib import Path

from capsule import blocks, db, lease


def _board():
    p = Path(tempfile.mkdtemp()) / "k.db"
    db.create_board(p)
    return db.connect(p)


class OpenParent(unittest.TestCase):
    def test_returns_lowest_open_parent_id_ignoring_done_and_archived(self):
        conn = _board()
        done = db.new_task(conn, title="d", status="done")
        arch = db.new_task(conn, title="a", status="archived")
        running = db.new_task(conn, title="r", status="running")
        todo = db.new_task(conn, title="t", status="todo")
        child = db.new_task(conn, title="c", status="ready", parents=(done, arch, todo, running))
        self.assertEqual(db.open_parent(conn, child), min(running, todo))
        self.assertIsNone(db.open_parent(conn, done))      # no parents at all
        conn.close()

    def test_none_when_every_parent_is_terminal(self):
        conn = _board()
        done = db.new_task(conn, title="d", status="done")
        arch = db.new_task(conn, title="a", status="archived")
        child = db.new_task(conn, title="c", status="ready", parents=(done, arch))
        self.assertIsNone(db.open_parent(conn, child))
        conn.close()

    def test_real_caller_claim_refuses_while_a_parent_is_open(self):
        conn = _board()
        parent = db.new_task(conn, title="p", status="ready")
        child = db.new_task(conn, title="c", status="ready", parents=(parent,))
        self.assertIsNone(lease.claim(conn, child, ttl=60, role="implementer"))
        self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()[0], "todo")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        self.assertIsNotNone(lease.claim(conn, child, ttl=60, role="implementer"))
        conn.close()

    def test_no_second_copy_of_the_predicate_outside_db_and_lookout(self):
        """Z8: `db.open_parent` is the one home of the 'parent not done/archived' SQL. Scan every
        production `capsule/*.py` for the `status NOT IN ('done','archived')` predicate near
        `task_links` (whitespace, quoting, string splitting and table aliases normalised away, so a
        renamed constant like `_OPEN_Q` is caught). Exactly one exception: lookout.py's bulk
        child-to-parent map (`parents = {...}` in `collect`), because one query over every
        task_links row cannot be a per-task helper call (N queries per pass). The second is
        blocks.py `archive_blocker`: the opposite direction (unfinished CHILDREN of a parent), not
        the open-parent predicate."""
        root = Path(__file__).resolve().parents[1]
        found = {}
        for f in sorted(root.glob("*.py")):
            if f.name == "db.py":
                continue
            text = re.sub(r"[\s\"]+", "", f.read_text().lower())
            n = sum(1 for m in re.finditer(r"status notin\('done','archived'\)".replace(" ", ""), text)
                    if "task_links" in text[max(0, m.start() - 300):m.end() + 300])
            if n:
                found[f.name] = n
        self.assertEqual(found, {"blocks.py": 1, "lookout.py": 1})

    def test_screen_fix_on_a_card_with_an_open_parent_lands_todo_not_ready(self):
        conn = _board()
        parent = db.new_task(conn, title="p", status="todo")
        child = db.new_task(conn, title="c", status="done", branch="epic/ui", parents=(parent,))
        blocks.hold_first_screen(conn, child, "epic/ui", screens=["a.html"], commit="abc")
        self.assertTrue(blocks.answer_first_screen(conn, child, "screen-fix", "redo"))
        self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()[0], "todo")
        conn.close()

    def test_screen_fix_with_no_open_parent_lands_ready(self):
        conn = _board()
        child = db.new_task(conn, title="c", status="done", branch="epic/ui")
        blocks.hold_first_screen(conn, child, "epic/ui", screens=["a.html"], commit="abc")
        self.assertTrue(blocks.answer_first_screen(conn, child, "screen-fix", "redo"))
        self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()[0], "ready")
        conn.close()


if __name__ == "__main__":
    unittest.main()
