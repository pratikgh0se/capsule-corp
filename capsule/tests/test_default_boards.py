"""M253: in Capsule 2.0 a board is made by `db.create_board(paths.board_db(...))` (see
docs/architecture/components/board-store.md); the file it makes is one status can open."""
import tempfile
import unittest
from pathlib import Path

from capsule import db, paths, statusview

NOW = 1_800_000_000


class CreateBoard(unittest.TestCase):
    def test_create_board_makes_the_board_status_can_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            db.create_board(paths.board_db("my-app", home))
            snap = statusview.snapshot(home, now=NOW, boards=["my-app"],
                                       events_path=home / "none.jsonl")
            text = " ".join(a["what"] for a in snap["attention"]["open"])
            self.assertNotIn("my-app: the board cannot be opened", text)


if __name__ == "__main__":
    unittest.main()
