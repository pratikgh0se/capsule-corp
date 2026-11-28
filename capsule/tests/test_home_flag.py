# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   HomeFlag.test_report_has_no_old_home_flag, HomeFlag.test_unblocked_has_no_old_home_flag
"""`--capsule-home` is honoured by `report` and `unblocked` (t_c8e8d478).

Each test runs the real `python3 -m capsule <cmd>` in a subprocess with `--capsule-home`
pointing at a NAMED temp home that holds distinguishable state, while `CAPSULE_HOME` points
at a different DECOY home holding different state. A command that ignored the flag would read
the decoy and the assertions below would fail."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule.tests.private_names import retired_only, retired_word
from capsule import db, paths


def _create_board(path):
    """A Capsule board db plus the `config.json` that makes `capsule unblocked` read it (D244)."""
    db.create_board(path)
    cfg = path.parent / "config.json"
    if not cfg.exists():
        cfg.write_text("{}")


class HomeFlag(unittest.TestCase):
    def setUp(self):
        self.named, self.decoy, self.legacy = (self._tmp() for _ in range(3))

    def _tmp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name)

    def run_capsule(self, *argv):
        env = {**os.environ, "CAPSULE_HOME": str(self.decoy),
               "LEGACY_KANBAN_HOME": str(self.legacy)}
        cmd = [sys.executable, "-m", "capsule", *argv, "--capsule-home", str(self.named)]
        return subprocess.run(cmd,
                              cwd=paths.REPO, env=env,
                              capture_output=True, text=True, timeout=60)

    def make_board(self, home, board, titles, runs=0):
        p = paths.board_db(board, home)
        _create_board(p)
        conn = db.connect(p)
        for title in titles:
            tid = db.new_task(conn, title=title, status="ready", now=10)
            for _ in range(runs):
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, started_at,"
                    " ended_at) VALUES (?, 'implementer', 'done', 'completed', 10, 20)", (tid,))
        conn.commit()
        conn.close()

    def test_handover_is_an_unknown_verb(self):
        r = self.run_capsule("handover", "take", "my-app")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("invalid choice", r.stderr)
        self.assertFalse((self.named / "handovers.jsonl").exists())

    @retired_only
    def test_report_has_no_old_home_flag(self):
        flag = f"--{retired_word()}-home"
        r = self.run_capsule("report", "my-app", flag, str(self.legacy))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(flag, r.stderr)

    def test_report_reads_the_named_homes_board(self):
        self.make_board(self.named, "my-app", ["a", "b"], runs=1)
        self.make_board(self.decoy, "my-app", ["c", "d", "e"], runs=2)
        r = self.run_capsule("report", "my-app")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["totals"]["runs"], 2)
        self.assertEqual(sorted(c["title"] for c in doc["cards"]), ["a", "b"])

    def test_unblocked_reads_the_named_homes_config_and_board(self):
        for home, board, title in ((self.named, "named-board", "card from the named home"),
                                   (self.decoy, "decoy-board", "card from the decoy home")):
            self.make_board(home, board, [title])
            (home / "config.json").write_text(
                json.dumps({"unblocked": {"boards": [board], "repos": []}}))
        r = self.run_capsule("unblocked", "--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)    # one unowned card
        titles = [i["item"] for i in json.loads(r.stdout)["items"]]
        self.assertEqual(len(titles), 1, titles)
        self.assertIn("named-board", titles[0])
        self.assertIn("card from the named home", titles[0])

    @retired_only
    def test_unblocked_has_no_old_home_flag(self):
        flag = f"--{retired_word()}-home"
        r = self.run_capsule("unblocked", "--json", flag, str(self.legacy))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(flag, r.stderr)


if __name__ == "__main__":
    unittest.main()
