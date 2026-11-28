"""Off-legacy C05, oracle 2: two real `Tick` instances over two temp boards append to the Lookout
concurrently for 200 passes each; no line is lost or torn.
Oracle 1: the counts each Tick returns. Oracle 2: the spool files read back line by line."""
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from capsule import db, paths, tick
from capsule.tests.test_tick import FakeEvents, FakeRunner

PASSES = 200
BOARDS = ("other-app", "capsule-corp")


class TwoTicksOneHome(unittest.TestCase):
    def test_two_ticks_append_concurrently_and_lose_no_line(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        base = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        ticks = {}
        for board in BOARDS:
            d = paths.board_dir(board, home)
            d.mkdir(parents=True)
            (d / "config.json").write_text(json.dumps(dict(base, board=board)))
            db.create_board(d / "kanban.db")
            runner = FakeRunner()
            runner.up = False
            ticks[board] = tick.Tick(home, board=board, runners=lambda t, r, rn=runner: rn,
                                     events=FakeEvents(), alive=lambda pid: False)
        gate, done, errors = threading.Barrier(len(BOARDS)), {}, []

        def run(board):
            gate.wait()
            try:
                done[board] = [ticks[board].once() for _ in range(PASSES)]
            except Exception as exc:   # surfaced by the assertion below, not swallowed
                errors.append((board, repr(exc)))

        threads = [threading.Thread(target=run, args=(b,)) for b in BOARDS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for board in BOARDS:
            self.assertEqual(sum(c["lookout_errors"] for c in done[board]), 0)
            text = (home / "lookout" / board / "spool.jsonl").read_text()
            self.assertTrue(text.endswith("\n"))
            docs = [json.loads(line) for line in text.splitlines()]   # a torn line raises here
            marks = [d for d in docs if d["t"] == "tickmark"]
            self.assertEqual(len(marks), PASSES)
            self.assertEqual({d.get("board") or d["row"]["board"] for d in docs}, {board})
        self.assertFalse((home / "lookout" / "spool.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
