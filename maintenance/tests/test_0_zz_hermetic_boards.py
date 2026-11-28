# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   HermeticBoards.test_the_default_boards_dir_is_not_the_live_legacy_one
"""Sorts early: discovery points `board.BOARDS_DIR` at an empty temp dir before any test (C29).

The write fence stats `<boards_dir>/<board>/.capsule-owned`. A test that opens a Board with a fake
runner and no `boards_dir=` would otherwise stat this host's live boards, which carry the marker
after the take, and every such test would then fail on the host. Process-wide on purpose: `board`
is shared by every test file `unittest discover` loads."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# `events.SPOOL` binds on its first import (here, via `board`), while later test files read
# EVENTS_SPOOL from the environment at test time. Set it first so the two always agree.
os.environ.setdefault("EVENTS_SPOOL", tempfile.mkdtemp(prefix="events-hermetic-") + "/events.jsonl")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import board  # noqa: E402
from capsule import legacy_names as _LN  # noqa: E402

# some tests assert the db path ends in "/boards/<b>/kanban.db"
board.BOARDS_DIR = Path(tempfile.mkdtemp(prefix="hermetic-")) / "boards"
board.BOARDS_DIR.mkdir()


class HermeticBoards(unittest.TestCase):
    def test_the_default_boards_dir_is_not_the_live_legacy_one(self):
        live = Path.home() / f".{_LN.NAME}"
        self.assertNotIn(live, board.BOARDS_DIR.parents)
        self.assertEqual(list(board.BOARDS_DIR.iterdir()), [])

    def test_a_default_board_reads_its_marker_from_the_hermetic_dir(self):
        probe = board.BOARDS_DIR / "hermetic-probe"
        probe.mkdir()
        self.addCleanup(shutil.rmtree, probe)
        (probe / ".capsule-owned").write_text("")
        b = board.open_board("hermetic-probe", runner=lambda argv: "")
        with self.assertRaises(board.BoardFrozen):
            b.comment("t_1", "x")


if __name__ == "__main__":
    unittest.main()
