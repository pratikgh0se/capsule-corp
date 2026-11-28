# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   BoardSeam.test_a_fake_board_creates_comments_blocks_and_reads_a_card
"""Task 4: no-model checks of the board seam and the fake board's guards, against a scripted
in-memory fake board. No subprocess, no docker, no network, no model. The merge-gate
walk-through that lived here was retired with the per-card legacy pipeline (D238)."""
import importlib.util
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["AGENT_OPS_NO_SEND"] = "1"
os.environ.setdefault("EVENTS_SPOOL", str(Path(tempfile.mkdtemp(prefix="events-e2e-")) / "events.jsonl"))
os.environ.setdefault("MERGE_GATE_STATE", tempfile.mkdtemp(prefix="gate-state-e2e-"))
os.environ.setdefault("OPS_BUDGET_STATE", tempfile.mkdtemp(prefix="budgets-e2e-"))

import board  # noqa: E402
import board_bridge as bb  # noqa: E402
# maintenance/tests is a package now (tmpclean __init__), so simplicity rule 1 no longer counts
# `fakes` as a bare repo module; load it by path and register it so `from fakes import` still works.
_FAKES_SPEC = importlib.util.spec_from_file_location(
    "fakes", Path(__file__).resolve().parent / "fakes.py")
fakes = importlib.util.module_from_spec(_FAKES_SPEC)
sys.modules["fakes"] = fakes
_FAKES_SPEC.loader.exec_module(fakes)
FakeLegacyCli = fakes.FakeLegacyCli

# Never the real host boards dir: patch board.BOARDS_DIR for this module's own test run only
# (`board` is shared process-wide across every test file `unittest discover` loads).
_ORIG_BOARDS_DIR = None


def setUpModule():
    global _ORIG_BOARDS_DIR
    _ORIG_BOARDS_DIR = board.BOARDS_DIR
    board.BOARDS_DIR = Path(tempfile.mkdtemp(prefix="gate-boards-e2e-"))


def tearDownModule():
    board.BOARDS_DIR = _ORIG_BOARDS_DIR


FIX = Path(__file__).parent / "fixtures" / "e2e"


def _vault(tmp):
    v = Path(tmp) / "vault"
    (v / "Specs").mkdir(parents=True)
    (v / "Boards").mkdir()
    (v / "Specs" / "soft-tick.md").write_text((FIX / "spec.md").read_text())
    return v


def _boards_dir(tmp):
    boards = Path(tmp) / "boards"
    (boards / "my-app").mkdir(parents=True)
    (boards / "my-app" / "kanban.db").write_bytes(b"")
    return boards


class Guards(unittest.TestCase):
    """The fake must refuse what the board itself refuses,
    so a test built on it can never pass on a sequence the board would have
    rejected."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="e2e-guards-")
        self.v = _vault(self.tmp)
        self.boards = _boards_dir(self.tmp)
        self._old_repo_path = bb.REPO_PATHS.get("my-app")
        bb.REPO_PATHS["my-app"] = self.tmp
        os.environ["LEGACY_KANBAN_BOARD"] = "my-app"

    def tearDown(self):
        if self._old_repo_path is None:
            bb.REPO_PATHS.pop("my-app", None)
        else:
            bb.REPO_PATHS["my-app"] = self._old_repo_path
        os.environ.pop("LEGACY_KANBAN_BOARD", None)

    def _dispatch(self):
        h = FakeLegacyCli(self.boards)
        impl, fin = bb.dispatch("soft-tick", specs_dir=self.v / "Specs", runner=h)
        return h, impl, fin

    def test_second_request_review_on_a_review_card_is_refused(self):
        """kanban_db.request_review (~line 6620): only status IN ('running','ready')."""
        h, impl, fin = self._dispatch()
        h._request_review(impl, "reviewer")
        self.assertEqual(h.show(impl)["task"]["status"], "review")
        with self.assertRaises(ValueError):
            h._request_review(impl, "reviewer")

    def test_unblock_on_a_non_blocked_card_is_refused(self):
        """kanban_db.unblock_task requires the card to be blocked/scheduled."""
        h, impl, fin = self._dispatch()
        self.assertEqual(h.show(impl)["task"]["status"], "ready")
        with self.assertRaises(ValueError):
            h._unblock(impl)

    def test_request_changes_without_a_claimed_review_run_is_refused(self):
        """kanban_db.request_changes (~line 6663-6779): valid only for a run
        claimed from 'review' (status 'running' with a claimed event whose
        source_status == 'review', checked at ~line 6711). A card merely
        sitting in 'review' - never claimed by the reviewer - must be
        refused, not silently treated as changes-requested."""
        h, impl, fin = self._dispatch()
        h._request_review(impl, "reviewer")
        self.assertEqual(h.show(impl)["task"]["status"], "review")
        with self.assertRaises(ValueError):
            h._request_changes(impl, "no")

    def test_complete_from_an_invalid_status_is_refused(self):
        """kanban_db.complete_task (~line 5363): accepts running|ready|blocked|
        review -> done. A card still gated on an undone parent ('todo') must
        be refused."""
        h, impl, fin = self._dispatch()
        self.assertEqual(h.show(fin)["task"]["status"], "todo")  # verify (qa) parent not done yet
        with self.assertRaises(ValueError):
            h._complete(fin, "nope")


class BoardSeam(unittest.TestCase):
    """The Board API drives the fake board exactly as the CLI argv did, and the
    fake still refuses what the board refuses."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="e2e-board-")
        self.boards = _boards_dir(self.tmp)

    def test_a_fake_board_creates_comments_blocks_and_reads_a_card(self):
        import board as bd
        from fakes import fake_board
        b, h = fake_board("my-app", boards_dir=self.boards)
        tid = b.create(bd.NewCard(title="Soft tick", body="x", assignee="dev",
                                  workspace="worktree:/w", idempotency_key="sprint:soft-tick"))
        b.comment(tid, "gate: hello")
        card = b.show(tid)
        self.assertEqual(card.id, tid)
        self.assertEqual(card.assignee, "dev")
        self.assertEqual(card.legacy_status, "ready")
        self.assertEqual([c["body"] for c in card.comments], ["gate: hello"])
        self.assertEqual(b.list(statuses=bd.LIVE_STATUSES)[0].id, tid)
        self.assertEqual(h.calls[0][2], "kanban")

    def test_the_fake_board_still_refuses_a_block_the_board_would_refuse(self):
        import board as bd
        from fakes import fake_board
        b, h = fake_board("my-app", boards_dir=self.boards)
        tid = b.create(bd.NewCard(title="T"))
        b.complete(tid, "done")
        with self.assertRaises(bd.TransitionRefused):
            b.block(tid, "repair: retry 1/3 at 10:00 (crashed)", kind="transient")
        self.assertNotIn("block", [a[5] for a in h.calls if len(a) > 5])

    def test_an_idempotency_key_returns_the_same_id(self):
        import board as bd
        from fakes import fake_board
        b, _ = fake_board("my-app", boards_dir=self.boards)
        card = bd.NewCard(title="T", idempotency_key="sprint:x")
        self.assertEqual(b.create(card), b.create(card))


if __name__ == "__main__":
    unittest.main()
