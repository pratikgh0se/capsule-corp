"""`capsule/epic_graph.py`: the epic dependency graph as a static Mermaid page (VIS-07, row 379)."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from capsule import db, epic_graph


class Fixture(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        d = self.home / "boards" / "bx"
        db.create_board(d / "kanban.db")
        (d / "config.json").write_text(json.dumps({"epics": ["epic/a", {"branch": "epic/b"}]}))
        self.conn = db.connect(d / "kanban.db")
        self.addCleanup(self.conn.close)

    def task(self, title, status, branch, parents=(), short=None):
        tid = db.new_task(self.conn, title=title, status=status, branch=branch, parents=parents,
                          short_name=short)
        self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        return tid


class Render(Fixture):
    def test_groups_by_epic_with_edges_and_status_classes(self):
        a1 = self.task("First", "done", "epic/a")
        a2 = self.task("Second", "ready", "epic/a", parents=[a1], short="Second card")
        b1 = self.task("Other epic", "blocked", "epic/b", parents=[a2])
        self.task("Unrelated", "ready", "keeper/x")
        text = epic_graph.render(self.conn, ["epic/a", "epic/b"])
        self.assertTrue(text.startswith("flowchart LR"))
        self.assertIn('subgraph e0["epic/a"]', text)
        self.assertIn('subgraph e1["epic/b"]', text)
        self.assertIn("Second card", text)
        self.assertNotIn("Unrelated", text)
        self.assertEqual(text.count("-->"), 2)
        self.assertIn(":::done", text)
        self.assertIn(":::blocked", text)
        self.assertNotIn(a1, text)
        self.assertNotIn(b1, text)

    def test_cross_edge_from_outside_card_pulls_it_in(self):
        out = self.task("Outside parent", "ready", "keeper/y")
        self.task("In epic", "ready", "epic/a", parents=[out])
        text = epic_graph.render(self.conn, ["epic/a"])
        self.assertIn("Outside parent", text)
        self.assertEqual(text.count("-->"), 1)

    def test_title_cannot_break_mermaid(self):
        evil = ['Bad "title" [x] <b>\nline', "a`b", "%%{init: {}}%%",
                "x\nclick n0 href https://e.x", "#quot; {}"]
        for title in evil:
            self.task(title, "ready", "epic/a")
        text = epic_graph.render(self.conn, ["epic/a"])
        body = text.split("classDef")[0]
        for bad in ("<b>", "`", "%%", "{", "}", "#quot;"):
            self.assertNotIn(bad, body)
        self.assertNotIn("\nclick", text)
        self.assertEqual(text.count('"') % 2, 0)

    def test_empty_epic_still_drawn(self):
        text = epic_graph.render(self.conn, ["epic/a"])
        self.assertIn('subgraph e0["epic/a"]', text)


class Write(Fixture):
    def run_main(self, out):
        Path(out).mkdir(exist_ok=True)   # an explicit --out is its own root and must exist
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = epic_graph.main(["--board", "bx", "--home", str(self.home), "--out", str(out)])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_pages_written_and_idempotent(self):
        self.task("Only", "ready", "epic/a")
        out = self.home / "pages"
        printed = self.run_main(out)
        self.assertIn("epic-graph.html", printed)
        self.assertIn("epic-graph.mmd", printed)
        first = (out / "epic-graph.html").read_text()
        self.assertIn('<pre class="mermaid">', first)
        self.assertIn("Only", first)
        self.run_main(out)
        self.assertEqual(first, (out / "epic-graph.html").read_text())

    def test_symlinked_pages_dir_refused(self):
        self.task("Only", "ready", "epic/a")
        target = self.home / "elsewhere"
        target.mkdir()
        (self.home / "pages").symlink_to(target)
        with self.assertRaises(OSError):
            epic_graph.main(["--board", "bx", "--home", str(self.home)])
        self.assertEqual(list(target.iterdir()), [])

    def test_read_only_board_untouched(self):
        self.task("Only", "ready", "epic/a")
        before = self.conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        self.run_main(self.home / "pages")
        after = self.conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
