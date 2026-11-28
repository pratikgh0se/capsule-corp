# mutation-check: targets-predate-range 9497d1fb6 -- recurrence test for inc_94760910; the fix
#   predates the range :: MainWaitRelease.test_a_sha_wait_is_released_by_the_tick
"""inc_94760910: a card held on a commit sha is released by the tick once the sha is in main,
with no hand unblock, no intervention event and no incident."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from capsule import blocks, db, incidents, main_waits


class MainWaitRelease(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, name):
        (self.repo / name).write_text(name)
        self.git("add", name)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
        return self.git("rev-parse", "HEAD")

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.repo = tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.on_main = self.commit("a")
        self.git("checkout", "-q", "-b", "side")
        self.off_main = self.commit("b")
        self.git("checkout", "-q", "main")
        db.create_board(tmp / "kanban.db")
        self.conn = db.connect(tmp / "kanban.db")
        self.addCleanup(self.conn.close)
        self.cfg = {"board": "t", "epics": [], "repo_url": str(self.repo)}

    def hold(self, sha):
        tid = db.new_task(self.conn, title="t", status="ready", branch="epic/x")
        self.assertEqual(blocks.wait(self.conn, tid, sha, declared=[]), "todo")
        return tid

    def row(self, tid):
        return tuple(self.conn.execute(
            "SELECT status, block_kind, waits_on_branch FROM tasks WHERE id=?", (tid,)).fetchone())

    def kinds(self, tid):
        return [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (tid,))]

    def test_a_sha_wait_is_released_by_the_tick(self):
        waiting, unknown, ready = (self.hold(s) for s in
                                   (self.off_main, "f" * 40, self.on_main))
        self.assertEqual(self.row(ready), ("todo", "dependency", self.on_main))
        incidents.from_events(self.conn, board="t", now=1000)   # sets the watermark
        main_waits.release_and_promote(self.conn, self.cfg, 1001)
        self.assertEqual(self.row(ready), ("todo", None, None))
        self.assertEqual(self.row(waiting), ("todo", "dependency", self.off_main))
        self.assertEqual(self.row(unknown), ("todo", "dependency", "f" * 40))
        released = [r for r in self.conn.execute(
            "SELECT task_id, payload FROM task_events WHERE kind='main_wait_released'")]
        self.assertEqual([r[0] for r in released], [ready])
        self.assertEqual(json.loads(released[0][1])["waits_on"], self.on_main)
        self.assertNotIn("intervention", self.kinds(ready))
        self.assertEqual(incidents.from_events(self.conn, board="t", now=1002), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM incidents").fetchone()[0], 0)
