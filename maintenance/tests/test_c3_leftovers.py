"""C3 guards-before-capability final-review leftovers (card t_797a5a48, row 127)."""
import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

os.environ["AGENT_OPS_NO_SEND"] = "1"
os.environ.setdefault("EVENTS_SPOOL", tempfile.mkdtemp(prefix="events-c3l-") + "/events.jsonl")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import decisions as dc
import test_contracts as tc
import board
import unittest.mock

def setUpModule():
    # Run alone, the real other-app board's `.capsule-owned` marker refuses every comment.
    global _boards
    fake = Path(tempfile.mkdtemp(prefix="c3l-boards-"))
    _boards = unittest.mock.patch.object(board, "BOARDS_DIR", fake)
    _boards.start()


def tearDownModule():
    _boards.stop()


class Scanner(unittest.TestCase):
    def test_glob(self):
        """L7: `**` and `/**` have an empty prefix, which matched no forbidden path."""
        real = tc.VaultJobWriteGuards.RUN_SH.read_text(encoding="utf-8")
        allow = tc._guard_allow(tc._guard_job_blocks(real)["inbox"])
        for planted in ("**", "/**"):
            text = real.replace(f'GUARD_ALLOW="{allow}"', f'GUARD_ALLOW="{allow}:{planted}"', 1)
            self.assertNotEqual(text, real)
            self.assertTrue(any(repr(planted) in f for f in tc.guard_findings(text)), planted)


class Shadow(unittest.TestCase):
    def test_answer_does_not_shadow_its_own_name(self):
        """L9."""
        self.assertNotIn("answer", dc.answer.__code__.co_varnames)


if __name__ == "__main__":
    unittest.main()
